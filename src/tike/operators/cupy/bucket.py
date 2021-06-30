__author__ = "Daniel Ching"
__copyright__ = "Copyright (c) 2020, UChicago Argonne, LLC."

from importlib_resources import files
from itertools import product

import cupy as cp
import numpy as np

from .operator import Operator
from .lamino import Lamino

_module = cp.RawModule(
    code=files('tike.operators.cupy').joinpath('bucket.cu').read_text())
_coords_weights_kernel = _module.get_function('coordinates_and_weights')
_bucket_fwd = _module.get_function('fwd')
_bucket_adj = _module.get_function('adj')


class Bucket(Lamino):
    """A Laminography operator.

    Laminography operators to simulate propagation of the beam through the
    object for a defined tilt angle. An object rotates around its own vertical
    axis, nz, and the beam illuminates the object some tilt angle off this
    axis.

    Attributes
    ----------
    n : int
        The pixel width of the cubic reconstructed grid.
    tilt : float32
        The tilt angle; the angle between the rotation axis of the object and
        the light source. π / 2 for conventional tomography. 0 for a beam path
        along the rotation axis.

    Parameters
    ----------
    u : (nz, n, n) complex64
        The complex refractive index of the object. nz is the axis
        corresponding to the rotation axis.
    data : (ntheta, n, n) complex64
        The complex projection data of the object.
    theta : array-like float32
        The projection angles; rotation around the vertical axis of the object.
    """

    def __init__(self, n, tilt, **kwargs):
        """Please see help(Lamino) for more info."""
        self.n = n
        self.tilt = np.float32(tilt)
        self.precision = np.int16(1)
        self.weight = np.float32(1.0 / self.precision**3)

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        pass

    def fwd(self, u: cp.array, theta: cp.array, **kwargs):
        """Perform forward laminography operation.

        Parameters
        ----------
        u : (nz, n, n) complex64
            The complex refractive index of the object. nz is the axis
            corresponding to the rotation axis.
        theta : array-like float32
            The projection angles; rotation around the vertical axis of the
            object.

        Return
        ------
        data : (ntheta, n, n) complex64
            The complex projection data of the object.

        """
        data = cp.zeros((len(theta), self.n, self.n), dtype='complex64')
        grid = self._make_grid().astype('int16')
        plane_coords = cp.zeros((len(grid), self.precision**3, 2),
                                dtype='int16')

        for t in range(len(theta)):
            assert grid.dtype == 'int16'
            assert self.tilt.dtype == 'float32'
            assert theta.dtype == 'float32'
            # assert type(t) == 'int'
            assert self.precision.dtype == 'int16'
            assert plane_coords.dtype == 'int16'
            _coords_weights_kernel(
                (grid.shape[0],),
                (self.precision, self.precision, self.precision),
                (
                    grid,
                    grid.shape[0],
                    self.tilt,
                    theta,
                    t,
                    self.precision,
                    plane_coords,
                ),
            )
            # Shift zero-centered coordinates to array indices; wrap negative
            # indices around
            plane_index = (plane_coords + self.n // 2) % self.n
            grid_index = (grid + self.n // 2) % self.n
            assert data.dtype == 'complex64'
            assert self.weight.dtype == 'float32'
            assert u.dtype == 'complex64', u.dtype
            assert grid_index.dtype == 'int16'
            assert plane_index.dtype == 'int16'
            assert self.precision.dtype == 'int16'
            _bucket_fwd(
                (grid.shape[0],),
                (self.precision**3,),
                (
                    data,
                    t,
                    data.shape[1],
                    data.shape[2],
                    self.weight,
                    u,
                    u.shape[0],
                    u.shape[1],
                    u.shape[2],
                    plane_index,
                    grid_index,
                    grid_index.shape[0],
                    self.precision,
                ),
            )
        return data

    def adj(self, data: cp.array, theta: cp.array, **kwargs):
        """Perform adjoint laminography operation.

        Parameters
        ----------
        data : (ntheta, n, n) complex64
            The complex projection data of the object.
        theta : array-like float32
            The projection angles; rotation around the vertical axis of the
            object.

        Return
        ------
        u : (nz, n, n) complex64
            The complex refractive index of the object. nz is the axis
            corresponding to the rotation axis.

        """
        u = cp.zeros((self.n, self.n, self.n), dtype='complex64')
        grid = self._make_grid().astype('int16')
        plane_coords = cp.zeros((len(grid), self.precision**3, 2),
                                dtype='int16')

        for t in range(len(theta)):
            _coords_weights_kernel(
                (grid.shape[0],),
                (self.precision, self.precision, self.precision),
                (
                    grid,
                    grid.shape[0],
                    self.tilt,
                    theta,
                    t,
                    self.precision,
                    plane_coords,
                ),
            )
            # Shift zero-centered coordinates to array indices; wrap negative
            # indices around
            plane_index = (plane_coords + self.n // 2) % self.n
            grid_index = (grid + self.n // 2) % self.n
            assert data.dtype == 'complex64'
            assert self.weight.dtype == 'float32'
            assert u.dtype == 'complex64'
            assert grid_index.dtype == 'int16'
            assert plane_index.dtype == 'int16'
            assert self.precision.dtype == 'int16'
            _bucket_adj(
                (grid.shape[0],),
                (self.precision**3,),
                (
                    data,
                    t,
                    data.shape[1],
                    data.shape[2],
                    self.weight,
                    u,
                    u.shape[0],
                    u.shape[1],
                    u.shape[2],
                    plane_index,
                    grid_index,
                    grid_index.shape[0],
                    self.precision,
                ),
            )
        return u

    def _make_grid(self):
        """Return integer coordinates in the grid; origin centered."""
        lo, hi = -self.n // 2, self.n // 2
        return cp.stack(
            cp.mgrid[lo:hi, lo:hi, lo:hi],
            axis=-1,
        ).reshape(self.n**3, 3)


def get_coordinates_and_weights(
    grid,
    tilt,
    theta,
    precision=1,
    plane_coords=None,
):
    """Return coordinates and weights of grid points projected onto plane.

    Assumes a grid with cells of unit size onto a plane with cells on unit
    size. For an arbitrary point, the coordinate of the cell
    that contains the point is comptued by floor(point). i.e. the coordinates
    of a grid width 4 would be [-2, -1, 0, 1]. i.e. the zeroth grid cell spans
    from [0, 1).

    Transformations defining the plane are rotations about the origin.

    Parameters
    ----------
    grid (N, 3) array int
        Coordinates of voxels on the grid. Coordinates are origin center.
    theta : (T,) float32
        The projection angles; rotation around the vertical axis of the object.
    tilt : float32
        The tilt angle; the angle between the rotation axis of the object and
        the light source. π / 2 for conventional tomography. 0 for a beam path
        along the rotation axis.
    precision : int
        The desired precision of the operator. Scales the number of grid
        samples by precision^3.

    Returns
    -------
    plane_coords(N, precision**3, 2): int
        The coordinates on the planes into which each grid point is projected.
    """
    N = len(grid)
    if plane_coords is None:
        plane_coords = cp.zeros((N, precision**3, 2), dtype='int')

    transformation = compute_transformation(tilt, theta)
    normal = transformation @ cp.array((1, 0, 0), dtype='float32')
    # print(f'python normal is {normal}')

    for cell in range(N):
        for i, j, k in product(range(precision), repeat=3):
            point = grid[cell] + cp.array([
                (i + 0.5) / precision,
                (j + 0.5) / precision,
                (k + 0.5) / precision,
            ])
            # print(f"python {point}")
            chunk = k + precision * (j + precision * i)
            p = project_point_to_plane(
                point,
                normal,
                transformation,
            )
            # print(f"python {p}")
            plane_coords[cell, chunk] = p

    return plane_coords


def compute_transformation(tilt, theta):
    """Return a transformation which aligns [1, 0, 0] with the plane normal."""
    transformation = cp.zeros((3, 3), dtype='float32')
    transformation[0, 0] = cp.cos(tilt)
    transformation[0, 1] = cp.sin(tilt)
    transformation[1, 0] = -cp.cos(theta) * cp.sin(tilt)
    transformation[1, 1] = cp.cos(theta) * cp.cos(tilt)
    transformation[1, 2] = -cp.sin(theta)
    transformation[2, 0] = -cp.sin(theta) * cp.sin(tilt)
    transformation[2, 1] = cp.sin(theta) * cp.cos(tilt)
    transformation[2, 2] = cp.cos(theta)
    return transformation


def project_point_to_plane(point, normal, transformation):
    """Return the integer coordinate of the point projected to the plane."""
    distance = cp.sum(point * normal)
    projected = point - distance * normal
    return cp.floor(transformation.T @ projected)[1:]