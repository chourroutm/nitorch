# Contract: NIfTI-Zarr Reading API

nitorch's volume I/O is reached through the same format-agnostic entry points for every
backend, so this feature's "interface contract" is: what a new nifti-zarr backend must
do to be a conforming `MappedArray`, plus the two additive capabilities (lazy array
access, level-fetch) this feature introduces.

## 1. Format-agnostic loading contract (unchanged call site)

```python
from nitorch.io import map, load

vol = map('scan.nii.zarr')     # or load(...) for eager data + affine
```

- No new argument, flag, or format-specific function is introduced at this call site
  (FR-001). A `.nii.zarr` (or `.zarr`) path is recognized automatically, the same way
  a `.nii`/`.mgz`/`.tiff` path already is (FR-002).
- `vol.affine`, `vol.voxel_size`, `vol.dtype`, etc. behave identically to loading the
  same content's plain NIfTI file (FR-003, SC-003).
- A path that is not a valid nifti-zarr store raises nitorch's existing "no reader could
  load this file" error, not a new/different error class (FR-005, SC-004).
- Every other already-supported format's behavior through this same entry point is
  unchanged (FR-006) — this is a regression contract, not just a new-feature one.

## 2. Lazy, chunked array access (new, additive)

```python
vol = map('scan.nii.zarr')
lazy = vol.as_dask()           # dask.array.Array, not yet computed
sub = lazy[64:128, 64:128, :]  # still lazy
data = sub.compute()           # only the overlapping chunks are read here
```

- `vol.data()`/`vol.fdata()` still work exactly as they do for every other format
  (eager, in-memory) — `.as_dask()` is additive, not a replacement (FR-004, FR-006).
- Slicing before `.compute()` MUST only read the on-disk chunks overlapping the
  requested sub-region (SC-002) — this is what makes User Story 2's "read a small
  sub-region of a store that doesn't fit in memory" scenario possible.

## 3. Resolution-level access (new, additive) — "Option A"

```python
vol = map('scan.nii.zarr')      # finest level, by default
n = vol.<nb-levels-property>    # number of native levels (1 for a single-scale store)
coarser = vol.<level-fetch>(1)  # a NiftiZarrArray bound to native level 1, same header
```

- Exact method/property names are an implementation detail for `tasks.md`; the contract
  is the *shape*: default construction is the finest level, and every other native level
  is reachable by explicit index, sharing the same header (FR-007).
- A single-scale nifti-zarr store simply reports one available level; no error or
  special-casing is required for the "no multiscale pyramid present" case.

## 4. Registration reuses native levels — "Integration Point 1" (User Story 3)

Not a new call site — an internal behavior change in `ImagePyramid`'s existing
level-construction loop (data-model.md), triggered automatically when `nitorch register`
(or any other `ImagePyramid` caller) is given a multiscale nifti-zarr input:

- For any requested registration level the store natively provides, the data used MUST
  be the store's own native data for that level, not a downsampled approximation of the
  finest level (FR-008, SC-005).
- For any requested level beyond what the store natively provides, nitorch MUST fall
  back to its existing downsampling, applied from the store's coarsest native level
  (FR-008).
- This MUST work without any new CLI flag or argument — `-l/--levels` behaves exactly as
  it already does; only the data backing each level changes when the input has native
  levels available.

## 5. Test contract

Each contract clause above MUST have a corresponding automated test (Constitution
Principle III):

- Loading a nifti-zarr store through `map()`/`load()` produces header metadata
  byte-for-byte equivalent to loading the same content's plain NIfTI file.
- Loading an invalid/incomplete nifti-zarr store raises nitorch's existing
  no-matching-reader error, not a bare/unhandled exception.
- `.as_dask()` returns an uncomputed array; slicing it before `.compute()` triggers
  reads of only the overlapping chunks (verifiable via a chunk-access counter/mock, or
  by confirming a sub-region read does not require the full store to fit in memory).
- Fetching a non-default level returns data matching that level's own native array, and
  shares the finest level's header.
- Selecting existing losses / running `nitorch register` on a non-nifti-zarr input is
  unaffected (regression guard for FR-006).
- Registering with `-l/--levels` against a multiscale nifti-zarr store uses native data
  for every level the store provides, and falls back to downsampling beyond that.
