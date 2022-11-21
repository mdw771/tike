"""Defines a free-space propagation operator based on the CuPy FFT module."""

__author__ = "Daniel Ching, Viktor Nikitin"
__copyright__ = "Copyright (c) 2020, UChicago Argonne, LLC."

import tike.ptycho.objective

from .cache import CachedFFT
from .operator import Operator


class Propagation(CachedFFT, Operator):
    """A Fourier-based free-space propagation using CuPy.

    Take an (..., N, N) array and apply the Fourier transform to the last two
    dimensions.

    Attributes
    ----------
    detector_shape : int
        The pixel width and height of the nearplane and farplane waves.
    model : string
        The type of noise model to use for the cost functions.
    cost : (data-like, farplane-like) -> float
        The function to be minimized when solving a problem.
    grad : (data-like, farplane-like) -> farplane-like
        The gradient of cost.

    Parameters
    ----------
    nearplane: (..., detector_shape, detector_shape) complex64
        The wavefronts after exiting the object.
    farplane: (..., detector_shape, detector_shape) complex64
        The wavefronts hitting the detector respectively.
        Shape for cost functions and gradients is
        (nscan, 1, 1, detector_shape, detector_shape).
    data, intensity : (nscan, detector_shape, detector_shape) complex64
        data is the square of the absolute value of `farplane`. `data` is the
        intensity of the `farplane`.

    """

    def __init__(self, detector_shape, model='gaussian', **kwargs):
        self.detector_shape = detector_shape
        self.cost = getattr(tike.ptycho.objective, f'{model}')
        self.grad = getattr(tike.ptycho.objective, f'{model}_grad')
        self.model = model

    def fwd(self, nearplane, overwrite=False, **kwargs):
        """Forward Fourier-based free-space propagation operator."""
        self._check_shape(nearplane)
        shape = nearplane.shape
        return self._fft2(
            nearplane.reshape(-1, self.detector_shape, self.detector_shape),
            norm='ortho',
            axes=(-2, -1),
            overwrite_x=overwrite,
        ).reshape(shape)

    def adj(self, farplane, overwrite=False, **kwargs):
        """Adjoint Fourier-based free-space propagation operator."""
        self._check_shape(farplane)
        shape = farplane.shape
        return self._ifft2(
            farplane.reshape(-1, self.detector_shape, self.detector_shape),
            norm='ortho',
            axes=(-2, -1),
            overwrite_x=overwrite,
        ).reshape(shape)

    def _check_shape(self, x):
        assert type(x) is self.xp.ndarray, type(x)
        shape = (-1, self.detector_shape, self.detector_shape)
        if (__debug__ and x.shape[-2:] != shape[-2:]):
            raise ValueError(f'waves must have shape {shape} not {x.shape}.')
