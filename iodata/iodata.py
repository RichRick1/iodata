# IODATA is an input and output module for quantum chemistry.
# Copyright (C) 2011-2019 The IODATA Development Team
#
# This file is part of IODATA.
#
# IODATA is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 3
# of the License, or (at your option) any later version.
#
# IODATA is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, see <http://www.gnu.org/licenses/>
# --
"""Module for handling input/output from different file formats."""


import os
from typing import List, Tuple, Type, Iterator
from types import ModuleType
from fnmatch import fnmatch
from pkgutil import iter_modules
from importlib import import_module

import numpy as np

from .utils import LineIterator


__all__ = ['IOData', 'load_one', 'load_many', 'dump_one', 'dump_many']


def find_format_modules():
    """Return all file-format modules found with importlib."""
    result = []
    for module_info in iter_modules(import_module('iodata.formats').__path__):
        if not module_info.ispkg:
            format_module = import_module('iodata.formats.' + module_info.name)
            if hasattr(format_module, 'patterns'):
                result.append(format_module)
    return result


format_modules = find_format_modules()


class ArrayTypeCheckDescriptor:
    """A type checker for IOData attributes."""

    def __init__(self, name: str, ndim: int = None, shape: Tuple = None, dtype: Type = None,
                 matching: List[str] = None, default: str = None, doc=None):
        """Initialize decorator to perform type and shape checking of np.ndarray attributes.

        Parameters
        ----------
        name
            Name of the attribute (without leading underscores).
        ndim
            The number of dimensions of the array.
        shape
            The shape of the array. Use -1 for dimensions where the shape is
            not fixed a priori.
        dtype
            The datatype of the array.
        matching
            A list of names of other attributes that must have consistent
            shapes. This argument requires that the shape is specified.
            All dimensions for which the shape tuple equals -1 are must be
            the same in this attribute and the matching attributes.
        default
            The name of another (type-checked) attribute to return as default
            when this attribute is not set

        """
        if matching is not None and shape is None:
            raise TypeError('The matching argument requires the shape to be specified.')

        self._name = name
        self._ndim = ndim
        self._shape = shape
        if dtype is None:
            self._dtype = None
        else:
            self._dtype = np.dtype(dtype)
        self._matching = matching
        self._default = default
        self.__doc__ = doc or 'A type-checked attribute'

    def __get__(self, instance, owner):
        if instance is None:
            return self
        if self._default is not None and not hasattr(instance, '_' + self._name):
            # When the attribute is not present, we assign it first with the
            # default value. The return statement can then remain completely
            # general.
            default = (getattr(instance, '_' + self._default).astype(self._dtype))
            setattr(instance, '_' + self._name, default)
        return getattr(instance, '_' + self._name)

    def __set__(self, obj, value):
        # try casting to proper dtype:
        value = np.array(value, dtype=self._dtype, copy=False)
        # if not isinstance(value, np.ndarray):
        #    raise TypeError('Attribute \'%s\' of \'%s\' must be a numpy '
        #                    'array.' % (self._name, type(obj)))
        if self._ndim is not None and value.ndim != self._ndim:
            raise TypeError(f"Attribute '{self._name}' of '{type(obj)}' must be a numpy array "
                            f"with {self._ndim} dimension(s).")
        if self._shape is not None:
            for i in range(len(self._shape)):
                if self._shape[i] >= 0 and self._shape[i] != value.shape[i]:
                    raise TypeError(f"Attribute '{self._name}' of '{type(obj)}' must be a numpy"
                                    f" array {self._shape[i]} elements in dimension {i}.")
        if self._dtype is not None:
            if not issubclass(value.dtype.type, self._dtype.type):
                raise TypeError(f"Attribute '{self._name}' of '{type(obj)}' must be a numpy "
                                f"array with dtype '{self._dtype.type}'.")
        if self._matching is not None:
            for othername in self._matching:
                other = getattr(obj, '_' + othername, None)
                if other is not None:
                    for i in range(len(self._shape)):
                        if self._shape[i] == -1 and \
                                other.shape[i] != value.shape[i]:
                            raise TypeError(f"shape[{i}] of attribute '{self._name}' of "
                                            f"'{type(obj)}' in is incompatible with "
                                            f"that of '{othername}'.")
        setattr(obj, '_' + self._name, value)

    def __delete__(self, obj):
        delattr(obj, '_' + self._name)


class IOData:
    """A container class for data loaded from (or to be written to) a file.

    In principle, the constructor accepts any keyword argument, which is
    stored as an attribute. All attributes are optional. Attributes can be
    set are removed after the IOData instance is constructed. The following
    attributes are supported by at least one of the io formats:

    Type checked array attributes (if present)
    ------------------------------------------

    atcoords
        A (N, 3) float array with Cartesian coordinates of the atoms.

    atcorenums
        A (N,) float array with pseudo-potential core charges.

    atforces
        A (N, 3) float array with Cartesian forces on each atom.

    atfrozen
        A (N,) bool array with frozen atoms. (All atoms are free if this
        attribute is not set.)

    atmasses
        A (N,) float array with atomic masses

    atnums
        A (N,) int vector with the atomic numbers.

    cube_data
        A (K, L, M) array of data on a uniform grid (defined by ugrid).

    polar
        A (3, 3) matrix containing the dipole polarizability tensor.

    **Unspecified type (duck typing):**

    atcharges
        A dictionary where keys are names of charge definitions and values are
        arrays with atomic charges (size N).

    atffparams
        A dictionary with arrays of atomic force field parameters (typically
        non-bonded). Keys include 'charges', 'vdw_radii', 'sigmas', 'epsilons',
        'alphas' (atomic polarizabilities), 'c6s', 'c8s', 'c10s', 'buck_as',
        'buck_bs', 'lj_as', 'core_charges', 'valence_charges', 'valence_widths',
        etc. Not all of them have to be present, depending on the use case.

    athessian
        A (3*N, 3*N) array containing the energy Hessian w.r.t Cartesian atomic
        displacements.

    basisdef
        A basis set definition, i.e. a dictionary whose keys are symbols (of
        chemical elements), atomic numbers (similar to previous, str to make
        distinction with following) or an atom index (integer referring to a
        specific atom in a molecule). The format of the values is to be decided
        when implementing a load function for basis set definitions.

    bonds
        An (nbond, 3) array with the list of covalent bonds. Each row represents
        one bond and consists of three integers: first atom index (starting
        from zero), second atom index & an optional bond type (0: not known, 1:
        single, 2: double, 3: triple, 4: conjugated).

    cellvecs
        A (NP, 3) array containing the (real-space) cell vectors describing
        periodic boundary conditions. A single vector corresponds to a 1D cell,
        e.g. for a wire. Two vectors describe a 2D cell, e.g. for a membrane.
        Three vectors describe a 3D cell, e.g. a crystalline solid.

    core_energy
        The Hartree-Fock energy due to the core orbitals

    energy
        The total energy (electronic + nn)

    extcharges
        Array with values of external charges, with shape (nextcharge, 4). First
        three columns for Cartesian X, Y and Z coordinates, last column for the
        actual charge.

    g_rot
        The rotational symmetry number of the molecule.

    mo
        An instance of MolecularOrbitals.

    nelec
        The number of electrons.

    obasis
        An OrderedDict containing parameters to instantiate a GOBasis class.

    obasis_name
        A name or DOI describing the basis set used for the orbitals in the
        mo attribute (if applicable). Should be consistent with
        www.basissetexchange.org.

    one_ints
        Dictionary where keys are names and values are numpy arrays with
        one-body operators, typically integrals of a one-body operator
        with a pair of (Gaussian) basis functions. Names can start with ``olp``
        (overlap), ``kin`` (kinetic energy), ``na`` (nuclear attraction),
        ``core`` (core hamiltonian), etc. When relevant, these names must have a
        suffix ``_ao`` or ``_mo`` to clarify in which basis the integrals are
        computed. ``_ao`` is used to denote integrals in a non-orthogonal
        (atomic orbital) basis. ``_mo`` is used to denote an orthogonal
        (molecular orbital) basis. For the overlap integrals, this suffix can be
        omitted because it is only useful to compute them in the atomic-orbital
        basis.

    one_rdms
        Dictionary where keys are names and values are one-particle density
        matrices. Names can be ``scf``, ``post_scf``, ``scf_spin``,
        ``post_scf_spin``. These matrices are always expressed in the AO basis.

    run_type
        The type of calculation that lead to the results stored in IOData, e.g.
        'energy', 'energy_force', 'opt', 'freq', ...

    spinpol
        The spin polarization. By default, its value is derived from the
        molecular orbitals (mo attribute), as abs(nalpha - nbeta). In this case,
        spinpol cannot be set. When no molecular orbitals are present, this
        attribute can be set.

    title
         A suitable name for the data.

    two_ints
        Dictionary where keys are names and values are numpy arrays with
        two-body operators, typically integrals of two-body operator
        with four of (Gaussian) basis functions. Names can start with ``er``
        (electron repulsion) or ``two`` (general pairswise interaction). When
        relevant, these names must have a suffix ``_ao`` or ``_mo`` to clarify
        in which basis the integrals are computed, see one_ints for more details.

    two_rdms
        Dictionary where keys are names and values are two-particle density
        matrices. Names can be ``post_scf`` or ``post_scf_spin``. These matrices
        are always expressed in the AO basis.

    ugrid
        A dictionary describing the uniform grid (typically from a cube file).
        It contains the following fields: ``origin``, a 3D vector with the
        origin of the axes frame. ``axes`` a 3x3 array where each row represents
        the spacing between two neighboring grid points along the first, second
        and third axis, respectively. ``shape`` A three-tuple with the number of
        points along each axis, respectively.

    """

    def __init__(self, **kwargs):
        """Initialize an IOData instance.

        All keyword arguments will be turned into corresponding attributes.
        """
        for key, value in kwargs.items():
            setattr(self, key, value)

    # only perform type checking on some attributes
    atcoords = ArrayTypeCheckDescriptor(
        'atcoords', 2, (-1, 3), float,
        ['atcorenums', 'atforces', 'atfrozen', 'atmasses', 'atnums'],
        doc="A (N, 3) float array with Cartesian coordinates of the atoms.")
    atcorenums = ArrayTypeCheckDescriptor(
        'atcorenums', 1, (-1,), float,
        ['atcoords', 'atforces', 'atfrozen', 'atmasses', 'atnums'],
        'atnums',
        doc="A (N,) float array with pseudo-potential core charges.")
    atforces = ArrayTypeCheckDescriptor(
        'atforces', 2, (-1, 3), float,
        ['atcoords', 'atcorenums', 'atfrozen', 'atmasses', 'atnums'],
        doc="A (N, 3) float array with Cartesian atomic forces.")
    atfrozen = ArrayTypeCheckDescriptor(
        'atfrozen', 1, (-1,), bool,
        ['atcoords', 'atcorenums', 'atforces', 'atmasses', 'atnums'],
        doc="A (N,) boolean array flagging fixed atoms.")
    atmasses = ArrayTypeCheckDescriptor(
        'atmasses', 1, (-1,), float,
        ['atcoords', 'atcorenums', 'atforces', 'atfrozen', 'atnums'],
        doc="A (N,) float array with atomic masses.")
    atnums = ArrayTypeCheckDescriptor(
        'atnums', 1, (-1,), int,
        ['atcoords', 'atcorenums', 'atforces', 'atfrozen', 'atmasses'],
        doc="A (N,) int vector with the atomic numbers.")
    cube_data = ArrayTypeCheckDescriptor(
        'cube_data', 3,
        doc="A (L, M, N) array of data on a uniform grid (defined by ugrid).")
    polar = ArrayTypeCheckDescriptor(
        'polar', 2, (3, 3), float,
        doc="A (3, 3) matrix containing the dipole polarizability tensor.")

    @property
    def natom(self) -> int:
        """Return the number of atoms."""
        if hasattr(self, 'atcoords'):
            return len(self.atcoords)
        if hasattr(self, 'atcorenums'):
            return len(self.atcorenums)
        if hasattr(self, 'atforces'):
            return len(self.atforces)
        if hasattr(self, 'atfrozen'):
            return len(self.atfrozen)
        if hasattr(self, 'atmasses'):
            return len(self.atmasses)
        if hasattr(self, 'atnums'):
            return len(self.atnums)
        raise AttributeError("Cannot determine the number of atoms.")

    @property
    def nelec(self) -> float:
        """Return the number of electrons."""
        mo = getattr(self, 'mo', None)
        if mo is None:
            return self._nelec
        return mo.nelec

    @nelec.setter
    def nelec(self, nelec: float):
        mo = getattr(self, 'mo', None)
        if mo is None:
            # We need to fix the following together with all the no-member
            # warnings, see https://github.com/theochem/iodata/issues/73
            # pylint: disable=attribute-defined-outside-init
            self._nelec = nelec
        else:
            raise TypeError("nelec cannot be set when orbitals are present.")

    @property
    def charge(self) -> float:
        """Return the net charge of the system."""
        atcorenums = getattr(self, 'atcorenums', None)
        if atcorenums is None:
            return self._charge
        return atcorenums.sum() - self.nelec

    @charge.setter
    def charge(self, charge: float):
        atcorenums = getattr(self, 'atcorenums', None)
        if atcorenums is None:
            # We need to fix the following together with all the no-member
            # warnings, see https://github.com/theochem/iodata/issues/73
            # pylint: disable=attribute-defined-outside-init
            self._charge = charge
        else:
            self.nelec = atcorenums.sum() - charge

    @property
    def spinpol(self) -> float:
        """Return the spin multiplicity."""
        mo = getattr(self, 'mo', None)
        if mo is None:
            return self._spinpol
        return mo.spinpol

    @spinpol.setter
    def spinpol(self, spinpol: float):
        mo = getattr(self, 'mo', None)
        if mo is None:
            # We need to fix the following together with all the no-member
            # warnings, see https://github.com/theochem/iodata/issues/73
            # pylint: disable=attribute-defined-outside-init
            self._spinpol = spinpol
        else:
            raise TypeError("spinpol cannot be set when orbitals are present.")


def _select_format_module(filename: str, attrname: str) -> ModuleType:
    """Find a file format module with the requested attribute name.

    Parameters
    ----------
    filename
        The file to load or dump.
    attrname
        The required atrtibute of the file format module.

    Returns
    -------
    format_module
        The module implementing the required file format.

    """
    basename = os.path.basename(filename)
    for format_module in format_modules:
        if any(fnmatch(basename, pattern) for pattern in format_module.patterns):
            if hasattr(format_module, attrname):
                return format_module
    raise ValueError('Could not find file format with feature {} for file {}'.format(
        attrname, filename))


def load_one(filename: str) -> IOData:
    """Load data from a file.

    This function uses the extension or prefix of the filename to determine the
    file format. When the file format is detected, a specialized load function
    is called for the heavy lifting.

    Parameters
    ----------
    filename
        The file to load data from.

    Returns
    -------
    out
        The instance of IOData with data loaded from the input files.

    """
    format_module = _select_format_module(filename, 'load')
    lit = LineIterator(filename)
    try:
        return IOData(**format_module.load(lit))
    except StopIteration:
        raise lit.error("File ended before all data was read.")


def load_many(filename: str) -> Iterator[IOData]:
    """Load multiple IOData instances from a file.

    This function uses the extension or prefix of the filename to determine the
    file format. When the file format is detected, a specialized load function
    is called for the heavy lifting.

    Parameters
    ----------
    filename
        The file to load data from.

    Yields
    ------
    out
        An instance of IOData with data for one frame loaded for the file.

    """
    format_module = _select_format_module(filename, 'load_many')
    lit = LineIterator(filename)
    for data in format_module.load_many(lit):
        try:
            yield IOData(**data)
        except StopIteration:
            return


def dump_one(iodata: IOData, filename: str):
    """Write data to a file.

    This routine uses the extension or prefix of the filename to determine
    the file format. For each file format, a specialized function is
    called that does the real work.

    Parameters
    ----------
    iodata
        The object containing the data to be written.
    filename : str
        The file to write the data to.

    """
    format_module = _select_format_module(filename, 'dump')
    with open(filename, 'w') as f:
        format_module.dump(f, iodata)


def dump_many(iodatas: Iterator[IOData], filename: str):
    """Write multiple IOData instances to a file.

    This routine uses the extension or prefix of the filename to determine
    the file format. For each file format, a specialized function is
    called that does the real work.

    Parameters
    ----------
    iodatas
        An iterator over IOData instances.
    filename : str
        The file to write the data to.

    """
    format_module = _select_format_module(filename, 'dump_many')
    with open(filename, 'w') as f:
        format_module.dump_many(f, iodatas)
