"""Functions related to creating and manipulating probe arrays.

Ptychographic probes are represented as two separate components: a shared probe
whose values are the same for all positions and the varying component. The
former is required as it provides the shared probe constraint for ptychography
and the later relaxes the former constraint to accomodate real-world
illuminations which may vary with time.

The shared component consist of a single array representing at least one probe
each of which may have an accompanying varying component.

The varying components are stored sparsely as two arrays, and the full
representation of the varying comonents are only combined as needed. The first
array is an array of eigen probes (principal components) spanning the space of
the probe variation of all positions and the second is an array of weights that
map the variation for each position into this space.

Each probe may have its own set of eigen probes. The unique probe at a given
position is reconstructed by adding the shared probe to a weighted sum of the
eigen probes.

.. code-block:: python

    varying_probe = weights[0] * probe + np.sum(weights[1:] * eigen_probes)


Design comments
---------------
In theory, the probe representation could be implemented in as little as two
arrays: one with all of the shared components where the probe becomes the first
eigen probe and and one with the weights. Choosing to keep the eigen probes
separate from the probe as a third array provides backwards compatability and
allows for storing fewer eigen probes in the case when only some probes are
allowed to vary.

"""

from __future__ import annotations
import copy
import dataclasses
import logging
import typing

import cupy as cp
import cupyx.scipy.ndimage
import numpy as np
import numpy.typing as npt

import tike.linalg
import tike.random
import tike.precision

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class ProbeOptions:
    """Manage data and setting related to probe correction."""

    recover_probe: bool = False
    """Boolean switch used to indicate whether to update probe or not."""

    update_start: int = 0
    """Start probe updates at this epoch."""

    update_period: int = 1
    """The number of epochs between probe updates"""

    init_rescale_from_measurements: bool = True
    """Initial rescaling of probe using measured intensity."""

    probe_photons: float = np.nan
    """The shared probe mode intensity must add up to this number.

    If we do not give a number for this in the parameters.toml file, then
    it will default to the average of the measurement intensity scaling.
    """

    force_orthogonality: bool = False
    """Forces probes to be orthogonal each iteration."""

    force_centered_intensity: bool = False
    """Forces the probe intensity to be centered."""

    force_sparsity: float = 0.0
    """Forces this proportion of zero elements."""

    use_adaptive_moment: bool = False
    """Whether or not to use adaptive moment."""

    vdecay: float = 0.999
    """The proportion of the second moment that is previous second moments."""

    mdecay: float = 0.9
    """The proportion of the first moment that is previous first moments."""

    v: typing.Union[npt.NDArray, None] = dataclasses.field(
        init=False,
        default_factory=lambda: None,
    )
    """The second moment for adaptive moment."""

    m: typing.Union[npt.NDArray, None] = dataclasses.field(
        init=False,
        default_factory=lambda: None,
    )
    """The first moment for adaptive moment."""

    probe_support: float = 0.0
    """Weight of the finite probe support constraint; zero or greater.

    This support constraint encourages round probes energy concentrated at the
    center of the probe grid. Higher support increases the effect.
    """

    probe_support_radius: float = 0.5 * 0.7
    """Radius of finite probe support as fraction of probe grid. [0.0, 0.5]."""

    probe_support_degree: float = 2.5
    """Degree of the supergaussian defining the probe support; zero or greater.

    Controls how hard the penalty transition is outside of the radius.
    Degree = 0 is a flat penalty.
    Degree > 0, < 1 is flatter than a gaussian.
    Degree 1 is a gaussian.
    Degree > 1 is more like a top-hat than a gaussian.
    """

    additional_probe_penalty: float = 0.0
    """Penalty applied to the last probe for existing.

    This penalty encourages the probe energy to concentrate in the lower order
    modes. The penalty starts at zero for the first probe and increases
    linearly to this value. For example, for three probes, the penalties aplied
    are [0.0, 0.5, 1.0].

    This is a soft constraint as opposed to `force_sparsity` which is a
    hard constraint.
    """

    probe_update_sum: typing.Union[npt.NDArray, None] = dataclasses.field(
        init=False,
        default_factory=lambda: None,
    )
    """Used for momentum updates."""

    preconditioner: typing.Union[npt.NDArray, None] = dataclasses.field(
        init=False,
        default_factory=lambda: None,
    )

    power: typing.List[typing.List[float]] = dataclasses.field(
        init=False,
        default_factory=list,
    )
    """The power of the primary probe modes at each iteration."""

    def copy_to_device(self, comm) -> ProbeOptions:
        """Copy to the current GPU memory."""
        options = copy.copy(self)
        if self.v is not None:
            options.v = cp.asarray(self.v)
        if self.m is not None:
            options.m = cp.asarray(self.m)
        if self.preconditioner is not None:
            options.preconditioner = comm.pool.bcast([self.preconditioner])
        return options

    def copy_to_host(self) -> ProbeOptions:
        """Copy to the host CPU memory."""
        options = copy.copy(self)
        if self.v is not None:
            options.v = cp.asnumpy(self.v)
        if self.m is not None:
            options.m = cp.asnumpy(self.m)
        if self.preconditioner is not None:
            options.preconditioner = cp.asnumpy(self.preconditioner[0])
        return options

    def resample(self, factor: float, interp) -> ProbeOptions:
        """Return a new `ProbeOptions` with the parameters rescaled."""
        options = ProbeOptions(
            recover_probe=self.recover_probe,
            update_start=self.update_start,
            update_period=self.update_period,
            init_rescale_from_measurements=self.init_rescale_from_measurements,
            probe_photons=self.probe_photons,
            force_orthogonality=self.force_orthogonality,
            force_centered_intensity=self.force_centered_intensity,
            force_sparsity=self.force_sparsity,
            use_adaptive_moment=self.use_adaptive_moment,
            vdecay=self.vdecay,
            mdecay=self.mdecay,
            probe_support=self.probe_support,
            probe_support_degree=self.probe_support_degree,
            probe_support_radius=self.probe_support_radius,
        )
        return options
        # Momentum reset to zero when grid scale changes


def get_varying_probe(shared_probe, eigen_probe=None, weights=None):
    """Construct the varying probes.

    Combines shared and eigen probes with weights to return a unique probe at
    each scanning position.

    Parameters
    ----------
    shared_probe : (..., 1,         1, SHARED, WIDE, HIGH) complex64
        The shared probes amongst all positions.
    eigen_probe :  (..., 1,     EIGEN, SHARED, WIDE, HIGH) complex64
        The eigen probes for all positions.
    weights :   (..., POSI, EIGEN + 1, SHARED) float32
        The relative intensity of the eigen probes at each position.

    Returns
    -------
    unique_probes : (..., POSI, 1, 1, WIDE, HIGH)
    """
    if weights is not None:
        # The zeroth eigen_probe is the shared_probe
        unique_probe = weights[..., [0], :, None, None] * shared_probe
        if eigen_probe is not None:
            # Not all shared_probes need have eigen probes
            m = eigen_probe.shape[-3]
            for c in range(eigen_probe.shape[-4]):
                unique_probe[..., :m, :, :] += (
                    weights[..., [c + 1], :m, None, None] *
                    eigen_probe[..., [c], :m, :, :])
        return unique_probe
    else:
        return shared_probe.copy()


def _constrain_variable_probe1(variable_probe, weights):
    """Help use the thread pool with constrain_variable_probe"""

    # Normalize variable probes
    vnorm = tike.linalg.mnorm(variable_probe, axis=(-2, -1), keepdims=True)
    variable_probe /= vnorm
    probes_with_modes = variable_probe.shape[-3]
    weights[..., 1:, :probes_with_modes] *= vnorm[..., 0, 0]

    # Orthogonalize variable probes
    variable_probe = tike.linalg.orthogonalize_gs(
        variable_probe,
        axis=(-2, -1),
        N=-4,
    )

    # Compute probe energy in order to sort probes by energy
    power = tike.linalg.norm(
        weights[..., 1:, :probes_with_modes],
        keepdims=True,
        axis=-3,
    )**2

    return variable_probe, weights, power


def _constrain_variable_probe2(variable_probe, weights, power):
    """Help use the thread pool with constrain_variable_probe"""

    # Sort the probes by energy
    probes_with_modes = variable_probe.shape[-3]
    for i in range(probes_with_modes):
        sorted = np.argsort(-power[..., i].flatten())
        weights[..., 1:, i] = weights[..., 1 + sorted, i]
        variable_probe[..., :, i, :, :] = variable_probe[..., sorted, i, :, :]

    # Remove outliars from variable probe weights
    aevol = cp.abs(weights)
    weights = cp.minimum(
        aevol,
        1.5 * cp.percentile(
            aevol,
            [95],
            axis=[-3],
            keepdims=True,
        ).astype(weights.dtype),
    ) * cp.sign(weights)

    return variable_probe, weights


def constrain_variable_probe(comm, variable_probe, weights):
    """Add the following constraints to variable probe weights

    1. Remove outliars from weights
    2. Enforce orthogonality once per epoch
    3. Sort the variable probes by their total energy
    4. Normalize the variable probes so the energy is contained in the weight

    """
    # TODO: No smoothing of variable probe weights yet because the weights are
    # not stored consecutively in device memory. Smoothing would require either
    # sorting and synchronizing the weights with the host OR implementing
    # smoothing of non-gridded data with splines using device-local data only.

    variable_probe, weights, power = zip(*comm.pool.map(
        _constrain_variable_probe1,
        variable_probe,
        weights,
    ))

    # reduce power by sum across all devices
    power = comm.pool.allreduce(power)

    variable_probe, weights = (list(a) for a in zip(*comm.pool.map(
        _constrain_variable_probe2,
        variable_probe,
        weights,
        power,
    )))

    return variable_probe, weights


def _get_update(R, eigen_probe, weights, batches, *, batch_index, c, m):
    # (..., POSI, 1, 1, 1, 1) to match other arrays
    weights = weights[..., batches[batch_index], c:c + 1, m:m + 1, None, None]
    eigen_probe = eigen_probe[..., c - 1:c, m:m + 1, :, :]
    norm_weights = tike.linalg.norm(weights, axis=-5, keepdims=True)**2

    if np.all(norm_weights == 0):
        raise ValueError('eigen_probe weights cannot all be zero?')

    # FIXME: What happens when weights is zero!?
    proj = (np.real(R.conj() * eigen_probe) + weights) / norm_weights
    return np.mean(
        R * np.mean(proj, axis=(-2, -1), keepdims=True),
        axis=-5,
        keepdims=True,
    )


def _get_d(patches, diff, eigen_probe, update, *, β, c, m):
    eigen_probe[..., c - 1:c, m:m + 1, :, :] += β * update / tike.linalg.mnorm(
        update,
        axis=(-2, -1),
        keepdims=True,
    )
    eigen_probe[..., c - 1:c, m:m + 1, :, :] /= tike.linalg.mnorm(
        eigen_probe[..., c - 1:c, m:m + 1, :, :],
        axis=(-2, -1),
        keepdims=True,
    )
    assert np.all(np.isfinite(eigen_probe))

    # Determine new eigen_weights for the updated eigen probe
    phi = patches * eigen_probe[..., c - 1:c, m:m + 1, :, :]
    n = np.mean(
        np.real(diff[..., m:m + 1, :, :] * phi.conj()),
        axis=(-1, -2),
        keepdims=False,
    )
    d = np.mean(np.square(np.abs(phi)), axis=(-1, -2), keepdims=False)
    d_mean = np.mean(d, axis=-3, keepdims=True)
    return eigen_probe, n, d, d_mean


def _get_weights_mean(n, d, d_mean, weights, batches, *, batch_index, c, m):
    # yapf: disable
    weight_update = (
        n / (d + 0.1 * d_mean)
    ).reshape(*weights[..., batches[batch_index], c:c + 1, m:m + 1].shape)
    # yapf: enable
    assert np.all(np.isfinite(weight_update))

    # (33) The sum of all previous steps constrained to zero-mean
    weights[..., batches[batch_index], c:c + 1, m:m + 1] += weight_update
    return weights


def update_eigen_probe(
    comm,
    R,
    eigen_probe,
    weights,
    patches,
    diff,
    batches,
    *,
    batch_index,
    β=0.1,
    c=1,
    m=0,
):
    """Update eigen probes using residual probe updates.

    This update is copied from the source code of ptychoshelves. It is similar
    to, but not the same as, equation (31) described by Odstrcil et al (2018).
    It is also different from updates described in Odstrcil et al (2016).
    However, they all aim to correct for probe variation.

    Parameters
    ----------
    comm : :py:class:`tike.communicators.Comm`
        An object which manages communications between both GPUs and nodes.
    R : (..., POSI, 1, 1, WIDE, HIGH) complex64
        Residual probe updates; what's left after subtracting the shared probe
        update from the varying probe updates for each position
    patches : (..., POSI, 1, 1, WIDE, HIGH) complex64
    diff : (..., POSI, 1, SHARED, WIDE, HIGH) complex64
    eigen_probe : (..., 1, EIGEN, SHARED, WIDE, HIGH) complex64
        The eigen probe being updated.
    β : float
        A relaxation constant that controls how quickly the eigen probe modes
        are updated. Recommended to be < 1 for mini-batch updates.
    weights : (..., POSI, EIGEN, SHARED) float32
        A vector whose elements are sums of the previous optimal updates for
        each posiiton.

    References
    ----------
    M. Odstrcil, P. Baksh, S. A. Boden, R. Card, J. E. Chad, J. G. Frey, W. S.
    Brocklesby, "Ptychographic coherent diffractive imaging with orthogonal
    probe relaxation." Opt. Express 24, 8360 (2016). doi: 10.1364/OE.24.008360

    Michal Odstrcil, Andreas Menzel, and Manuel Guizar-Sicaros. Iterative
    least-squares solver for generalized maximum-likelihood ptychography.
    Optics Express. 2018.
    """
    assert R[0].shape[-3] == R[0].shape[-4] == 1
    assert 1 == eigen_probe[0].shape[-5]
    assert R[0].shape[:-5] == eigen_probe[0].shape[:-5] == weights[0].shape[:-3]
    assert weights[0][..., batches[0][batch_index], :, :].shape[-3] == R[0].shape[-5]
    assert R[0].shape[-2:] == eigen_probe[0].shape[-2:]

    update = comm.pool.map(
        _get_update,
        R,
        eigen_probe,
        weights,
        batches,
        batch_index=batch_index,
        c=c,
        m=m,
    )
    update = comm.pool.bcast([comm.Allreduce_mean(
        update,
        axis=-5,
    )])

    (eigen_probe, n, d, d_mean) = (list(a) for a in zip(*comm.pool.map(
        _get_d,
        patches,
        diff,
        eigen_probe,
        update,
        β=β,
        c=c,
        m=m,
    )))

    d_mean = comm.pool.bcast([comm.Allreduce_mean(
        d_mean,
        axis=-3,
    )])

    weights = list(
        comm.pool.map(
            _get_weights_mean,
            n,
            d,
            d_mean,
            weights,
            batches,
            batch_index=batch_index,
            c=c,
            m=m,
        ))

    return eigen_probe, weights


def adjust_probe_power(probe, power=None):
    """Rescale the probes according to given power.

    If no power is given, then probes rescaled as 1/N.

    Parameters
    ----------
    probe : (..., M, :, :) array
        A probe with M > 0 incoherent modes.
    power : (..., M, ) array
        The relative power of the probe modes.
    """
    if power is None:
        power = 1.0 / np.arange(1, probe.shape[-3] + 1)
    power = power[..., None, None]

    norm = tike.linalg.norm(probe, axis=(-2, -1), keepdims=True)
    probe *= power * norm[..., 0:1, :, :] / norm
    return probe


def add_modes_random_phase(probe, nmodes):
    """Initialize additional probe modes by phase shifting the first mode.

    Parameters
    ----------
    probe : (..., M, :, :) array
        A probe with M > 0 incoherent modes.
    nmodes : int
        The number of desired modes.

    References
    ----------
    M. Odstrcil, P. Baksh, S. A. Boden, R. Card, J. E. Chad, J. G. Frey, W. S.
    Brocklesby, "Ptychographic coherent diffractive imaging with orthogonal
    probe relaxation." Opt. Express 24, 8360 (2016). doi: 10.1364/OE.24.008360
    """
    all_modes = np.empty_like(
        probe,
        shape=(*probe.shape[:-3], nmodes, *probe.shape[-2:]),
    )
    pw = probe.shape[-1]
    for m in range(nmodes):
        if m < probe.shape[-3]:
            # copy existing mode
            all_modes[..., m, :, :] = probe[..., m, :, :]
        else:
            # randomly shift the first mode
            shift = np.exp(-2j * np.pi * (np.random.rand(2, 1) - 0.5) *
                           ((np.arange(0, pw) + 0.5) / pw - 0.5))
            all_modes[..., m, :, :] = (probe[..., 0, :, :] * shift[0][None] *
                                       shift[1][:, None])
    return all_modes


def add_modes_cartesian_hermite(probe, nmodes: int):
    """Create more probes from a 2D Cartesian Hermite basis functions.

    Starting with the given probe, new modes are computed by multiplying it
    with a set of 2D Cartesian Hermite functions. The probes are then
    orthonormalized.

    Parameters
    ----------
    probe : (..., 1, WIDTH, HEIGHT)
        A single probe basis.
    nmodes : int > 0
        The number of desired probes.

    Returns
    -------
    probe : (..., nmodes, WIDTH, HEIGHT)
        New probes basis.

    References
    ----------
    Michal Odstrcil, Andreas Menzel, and Manuel Guizar-Sicaros. Iterative
    least-squares solver for generalized maximum-likelihood ptychography.
    Optics Express. 2018.
    """
    if nmodes < 1:
        raise ValueError(f"nmodes cannot be less than 1. It was {nmodes}.")
    if probe.ndim < 3:
        raise ValueError(f"probe is incorrect shape is should be "
                         " (..., 1, W, new_probes) not {probe.shape}.")

    M = int(np.ceil(np.sqrt(nmodes)))
    N = int(np.ceil(nmodes / M))

    X, Y = np.meshgrid(
        np.arange(probe.shape[-2]) - (probe.shape[-2] // 2 - 1),
        np.arange(probe.shape[-1]) - (probe.shape[-2] // 2 - 1),
        indexing='xy',
    )

    cenx = np.sum(
        X * np.abs(probe)**2,
        axis=(-2, -1),
        keepdims=True,
    ) / np.sum(
        np.abs(probe)**2,
        axis=(-2, -1),
        keepdims=True,
    )
    ceny = np.sum(
        Y * np.abs(probe)**2,
        axis=(-2, -1),
        keepdims=True,
    ) / np.sum(
        np.abs(probe)**2,
        axis=(-2, -1),
        keepdims=True,
    )

    varx = np.sum(
        (X - cenx)**2 * np.abs(probe)**2,
        axis=(-2, -1),
        keepdims=True,
    ) / np.sum(
        np.abs(probe)**2,
        axis=(-2, -1),
        keepdims=True,
    )
    vary = np.sum(
        (Y - ceny)**2 * np.abs(probe)**2,
        axis=(-2, -1),
        keepdims=True,
    ) / np.sum(
        np.abs(probe)**2,
        axis=(-2, -1),
        keepdims=True,
    )

    # Create basis
    new_probes = list()
    for nii in range(N):
        for mii in range(M):

            basis = ((X - cenx)**mii) * ((Y - ceny)**nii) * probe

            if not (mii == 0 and nii == 0):
                basis *= np.exp(
                    -((X - cenx)**2 / (2 * varx))
                    -((Y - ceny)**2 / (2 * vary))
                )  # yapf: disable

            basis /= tike.linalg.norm(basis, axis=(-2, -1), keepdims=True)

            for H in new_probes:
                basis -= H * tike.linalg.inner(
                    H,
                    basis,
                    axis=(-2, -1),
                    keepdims=True,
                )

            basis /= tike.linalg.norm(basis, axis=(-2, -1), keepdims=True)

            new_probes.append(basis)

            if len(new_probes) == nmodes:
                return np.concatenate(new_probes, axis=-3)[..., :nmodes, :, :]

    raise RuntimeError(
        "`add_modes_cartesian_hermite` never reached a return statement."
        " This should never happen.")


def simulate_varying_weights(scan, eigen_probe):
    """Generate weights for eigen probe that follow random sinusoid.

    The amplitude of the of weights is 1, the phase shift is (0, 2π], and the
    period is at most one full scan.
    """
    N = scan.shape[1]
    x = np.arange(N)[..., :, None, None]
    period = N * np.random.rand(*eigen_probe.shape[:-2])
    phase = 2 * np.pi * np.random.rand(*eigen_probe.shape[:-2])
    return np.sin(2 * np.pi / period * x - phase)


def init_varying_probe(
    scan,
    shared_probe,
    num_eigen_probes,
    probes_with_modes=1,
):
    """Initialize arrays varying probe / eigen probes.

    If num_eigen_probes is 1, then the shared probe is allowed to vary but no
    additional eigen probes are created.

    Parameters
    ----------
    shared_probe : (..., 1, 1, SHARED, WIDE, HIGH) complex64
        The shared probes amongst all positions.
    scan :  (..., POSI, 2) float32
        The eigen probes for all positions.
    num_eigen_probes : int
        The number of principal components used to represent the varying probe
        illumination.
    probes_with_modes : int
        The number of probes that are allowed to vary.

    Returns
    -------
    eigen_probe :  (..., 1, EIGEN - 1, probes_with_modes, WIDE, HIGH) complex64
        The eigen probes for all positions. None if EIGEN <= 1.
    weights :   (..., POSI,     EIGEN, SHARED) float32
        The relative intensity of the eigen probes at each position. None if
        EIGEN < 1.

    """
    probes_with_modes = max(probes_with_modes, 0)
    if probes_with_modes > shared_probe.shape[-3]:
        raise ValueError(
            f"probes_with_modes ({probes_with_modes}) cannot be more than "
            "the number of probes ({shared_probe.shape[-3]})!")
    if num_eigen_probes < 1:
        return None, None

    weights = 1e-6 * np.random.rand(
        *scan.shape[:-1],
        num_eigen_probes,
        shared_probe.shape[-3],
    ).astype(tike.precision.floating)
    weights -= np.mean(weights, axis=-3, keepdims=True)
    # The weight of the first eigen probe is non-zero.
    weights[..., 0, :] = 1.0
    # Set unused weights to NaN
    weights[..., 1:, probes_with_modes:] = 0

    if num_eigen_probes == 1:
        return None, weights

    eigen_probe = tike.random.numpy_complex(
        *shared_probe.shape[:-4],
        num_eigen_probes - 1,
        probes_with_modes,
        *shared_probe.shape[-2:],
    )
    # The eigen probes are mean normalized.
    eigen_probe /= tike.linalg.mnorm(eigen_probe, axis=(-2, -1), keepdims=True)

    return eigen_probe, weights


def orthogonalize_eig(
    x: npt.NDArray,) -> typing.Tuple[npt.NDArray, npt.NDArray]:
    """Orthogonalize modes of x using eigenvectors of the pairwise dot product.

    Parameters
    ----------
    x : (..., nmodes, :, :) array_like complex64
        An array of the probe modes vectorized

    Returns
    -------
    x : array_like
        The orthogonalized probes
    power : array_like
        The power of each probe

    References
    ----------
    M. Odstrcil, P. Baksh, S. A. Boden, R. Card, J. E. Chad, J. G. Frey, W. S.
    Brocklesby, "Ptychographic coherent diffractive imaging with orthogonal
    probe relaxation." Opt. Express 24, 8360 (2016). doi: 10.1364/OE.24.008360
    """
    xp = cp.get_array_module(x)
    nmodes = x.shape[-3]
    # 'A' holds the dot product of all possible mode pairs. This is equivalent
    # to x^H @ x. We only fill the upper half of `A` because it is
    # conjugate-symmetric.
    A = xp.empty_like(x, shape=(*x.shape[:-3], nmodes, nmodes))
    for i in range(nmodes):
        for j in range(i, nmodes):
            A[..., i, j] = xp.sum(
                x[..., i, :, :].conj() * x[..., j, :, :],
                axis=(-1, -2),
            )
    # We find the eigen vectors of x^H @ x in order to get v^H from SVD of x
    # without computing u, s.
    val, vectors = xp.linalg.eigh(A, UPLO='U')
    result = (vectors.swapaxes(-1, -2) @ x.reshape(*x.shape[:-2], -1)).reshape(
        *x.shape)
    power = np.square(tike.linalg.norm(result, axis=(-2, -1),
                                       keepdims=False)).flatten()
    order = np.argsort(power, axis=None, kind='stable')[::-1]
    result = result[..., order, :, :]
    power = power[order]
    return result, power


def gaussian(size, rin=0.8, rout=1.0):
    """Return a complex gaussian probe distribution.

    Illumination probe represented on a 2D regular grid.

    A finite-extent circular shaped probe is represented as
    a complex wave. The intensity of the probe is maximum at
    the center and damps to zero at the borders of the frame.

    Parameters
    ----------
    size : int
        The side length of the distribution
    rin : float [0, 1) < rout
        The inner radius of the distribution where the dampening of the
        intensity will start.
    rout : float (0, 1] > rin
        The outer radius of the distribution where the intensity will reach
        zero.

    """
    r, c = np.mgrid[:size, :size] + 0.5
    rs = np.sqrt((r - size / 2)**2 + (c - size / 2)**2)
    rmax = np.sqrt(2) * 0.5 * rout * rs.max() + 1.0
    rmin = np.sqrt(2) * 0.5 * rin * rs.max()
    img = np.zeros((size, size), dtype=tike.precision.floating)
    img[rs < rmin] = 1.0
    img[rs > rmax] = 0.0
    zone = np.logical_and(rs > rmin, rs < rmax)
    img[zone] = np.divide(rmax - rs[zone], rmax - rmin)
    return img


def constrain_center_peak(probe):
    """Force the peak illumination intensity to the center of the probe grid.

    After smoothing the intensity of the combined illumination with a gaussian
    filter with standard deviation sigma, the probe is shifted such that the
    maximum intensity is centered.
    """
    half = probe.shape[-2] // 2, probe.shape[-1] // 2
    logger.info(
        "Constrained probe intensity to center with sigma=%.3e",
        half[0],
    )
    # First reshape the probe to 3D so it is a single stack of 2D images.
    stack = probe.reshape((-1, *probe.shape[-2:]))
    intensity = cupyx.scipy.ndimage.gaussian_filter(
        input=np.sum(np.square(np.abs(stack)), axis=0),
        sigma=half,
        mode='wrap',
    )
    # Find the maximum intensity in 2D.
    center = np.argmax(intensity)
    # Find the 2D coordinates of the maximum.
    coords = cp.unravel_index(center, dims=probe.shape[-2:])
    # Shift each of the probes so the max is in the center.
    p = np.roll(stack, half[0] - coords[0], axis=-2)
    stack = np.roll(p, half[1] - coords[1], axis=-1)
    # Reform to the original shape; make contiguous.
    probe = stack.reshape(probe.shape)
    return probe


def constrain_probe_sparsity(probe, f):
    """Constrain the probe intensity so at least `f` fraction elements are zero."""
    if f == 0:
        return probe
    logger.info("Constrained probe intensity so %.3e percent are zero", f * 100)
    # First reshape the probe to 3D so it is a single stack of 2D images.
    stack = probe.reshape((-1, *probe.shape[-2:]))
    intensity = np.sum(np.square(np.abs(stack)), axis=0)
    sigma = probe.shape[-2] / 8, probe.shape[-1] / 8
    intensity = cupyx.scipy.ndimage.gaussian_filter(
        input=intensity,
        sigma=sigma,
        mode='wrap',
    )
    # Get the coordinates of the smallest k values
    k = int(f * probe.shape[-1] * probe.shape[-2])
    smallest = np.argpartition(intensity, k, axis=None)[:k]
    coords = cp.unravel_index(smallest, dims=probe.shape[-2:])
    # Set these k smallest values to zero in all probes
    probe[..., coords[0], coords[1]] = 0
    return probe


def finite_probe_support(probe, *, radius=0.5, degree=5, p=1.0):
    """Returns a supergaussian penalty function for finite probe support.

    A mask which provides an illumination penalty is determined by the equation:

    penalty = p - p * exp( -( (x / radius)**2 + (y / radius)**2 )**degree)

    where the maximum penalty is p and the minium penalty is 0. This penalty
    function is used in the probe gradient to supress values in the probe grid
    far from the center. The penalty is 0 near the center and p at the edge.


    Parameters
    ----------
    radius : float (0, 0.5]
        The radius of the supergaussian.
    degree : float >= 0
        The exponent of the terms in the supergaussian equation. Controls how
        hard the penalty transition is outside of the radius.
        Degree = 0 is a flat penalty.
        Degree > 0, < 1 is flatter than a gaussian.
        Degree 1 is a gaussian.
        Degree > 1 is more like a top-hat than a gaussian.
    """
    if p <= 0:
        return 0.0
    logger.info(
        "Probe support constraint with weight %.3e, radius %.3e, degree %.3e",
        p,
        radius,
        degree,
    )
    N = probe.shape[-1]
    centers = cp.linspace(-0.5, 0.5, num=N, endpoint=False) + 0.5 / N
    i, j = cp.meshgrid(centers, centers)
    mask = 1 - cp.exp(-(cp.square(i / radius) + cp.square(j / radius))**degree)
    return p * mask.astype(tike.precision.floating)


def rescale_probe_using_fixed_intensity_photons(
    probe,
    Nphotons,
    probe_power_fraction=None,
):
    """
    Rescale the shared probes so the sum of their intensities is Nphotons.

    Parameters
    ----------
    Nphotons : float (0, inf)
        The total number of photons in the shared probe mode intensity, i.e.
        the sum of the intensity of the shared probe modes.

    probe_power_fraction : array_like
        A vector of length N_p (N_p = number of shared probe modes) that
        contains the relative energy of each mode; must add up to 1.0
    """

    probe_photons = cp.sum(np.abs(probe)**2, (-1, -2))

    if probe_power_fraction is None:
        probe_power_fraction = probe_photons / cp.sum(probe_photons)

    probe = probe * cp.sqrt(probe_power_fraction * Nphotons / probe_photons)[..., None,
                                                                  None]

    return probe


if __name__ == "__main__":
    cp.random.seed()
    x = (cp.random.rand(7, 1, 9, 3, 3) +
         1j * cp.random.rand(7, 1, 9, 3, 3)).astype(tike.precision.cfloating)
    x1, _ = orthogonalize_eig(x)
    assert x1.shape == x.shape, x1.shape

    p = (cp.random.rand(3, 7, 7) * 100).astype(int)
    p1 = constrain_center_peak(p)
    print(p1)
    p2 = constrain_probe_sparsity(p1, 0.6)
    print(p2)

    import sys
    np.set_printoptions(threshold=sys.maxsize, precision=2)
    print(finite_probe_support(
        np.zeros((24, 24)),
        radius=0.5,
        degree=5,
    ))
