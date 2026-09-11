# Feature Specification: NIfTI-Zarr Volume Support

**Feature Branch**: `003-nifti-zarr-support`

**Created**: 2026-09-08

**Status**: Draft

**Input**: User description: "add support for nifti-zarr
(https://github.com/neuroscales/nifti-zarr-py) by extending the
nitorch/io/volumes to read these with a nifti header and an array as a dask
array"

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Load a nifti-zarr volume through the existing loading interface (Priority: P1)

As a user of nitorch's volume I/O, I want to load a nifti-zarr store the same
way I already load a NIfTI or MGH file, so that I can work with large,
chunked neuroimaging datasets without learning a new loading interface or
converting the data to a different format first.

**Why this priority**: This is the core value of the feature — without it,
nifti-zarr data cannot be used in nitorch at all. Every other capability
depends on a nifti-zarr store first being recognized and loadable.

**Independent Test**: Can be fully tested by pointing nitorch's existing
volume-loading entry point at a valid nifti-zarr store and confirming it is
recognized, loaded, and exposes the same orientation/voxel-size metadata a
NIfTI file of the same content would.

**Acceptance Scenarios**:

1. **Given** a valid nifti-zarr store on the local filesystem, **When** a
   user loads it through nitorch's existing volume-loading interface,
   **Then** the resulting object exposes the same header-derived metadata
   (orientation matrix, voxel size, data type) that loading the equivalent
   plain NIfTI file would expose.
2. **Given** a file path that is not a valid nifti-zarr store (or does not
   exist), **When** a user attempts to load it as one, **Then** nitorch
   reports a clear error rather than crashing or silently returning
   incorrect data, consistent with how other unsupported/invalid inputs are
   already handled.

---

### User Story 2 - Access nifti-zarr data without loading the whole volume into memory (Priority: P2)

As a user working with a large nifti-zarr dataset, I want the underlying
array to be accessed lazily and in chunks, so that I can read and process
data that would not fit entirely in memory, and only pay the cost of reading
the parts I actually use.

**Why this priority**: This is the specific advantage nifti-zarr offers over
plain NIfTI (chunked, lazy access), and is explicitly called out in the
request. It is secondary to User Story 1 because a user must first be able
to load the store at all before this access pattern matters.

**Independent Test**: Can be fully tested by loading a nifti-zarr store
larger than a small threshold, reading a small sub-region of it, and
confirming that only the corresponding chunk(s) were read from storage
rather than the entire array.

**Acceptance Scenarios**:

1. **Given** a loaded nifti-zarr store, **When** a user requests a small
   spatial sub-region of the data, **Then** only the on-disk chunks
   overlapping that sub-region are read.
2. **Given** a loaded nifti-zarr store, **When** a user requests the array
   representation of the full data, **Then** they receive a chunked, lazily
   evaluated array they can further slice, compute on, or materialize
   on-demand, rather than an array that was already fully materialized in
   memory when it was requested.

---

### Edge Cases

- What happens when a nifti-zarr store's NIfTI header metadata is missing,
  incomplete, or inconsistent with its array's shape/dtype?
- What happens when a nifti-zarr store contains multiple resolution levels
  (an OME-Zarr-style multiscale pyramid)?
- What happens when a nifti-zarr store is only partially written (e.g. an
  interrupted conversion), or a chunk file referenced by its metadata is
  missing?
- What happens when a user attempts to write/save to a nifti-zarr store
  rather than only reading one?
- What happens when the same file path could plausibly be matched by more
  than one registered volume reader (format ambiguity)?

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST allow a user to load a nifti-zarr store through
  nitorch's existing, format-agnostic volume-loading interface, without
  requiring a separate, format-specific loading call.
- **FR-002**: System MUST correctly recognize a valid nifti-zarr store as
  such (as opposed to a plain NIfTI file or an unrelated Zarr store) when
  presented to the loading interface.
- **FR-003**: System MUST expose the store's NIfTI header metadata
  (orientation matrix, voxel size, data type, and other metadata already
  exposed for other supported volume formats) after loading.
- **FR-004**: System MUST expose the store's array data as a chunked, lazily
  evaluated array, such that requesting a sub-region reads only the
  overlapping on-disk chunks rather than the entire array.
- **FR-005**: System MUST report a clear, actionable error when a path
  cannot be loaded as a nifti-zarr store (missing, invalid, or corrupt
  store), rather than crashing or returning incorrect data.
- **FR-006**: System MUST leave existing behavior for already-supported
  volume formats (NIfTI, MGH, TIFF, etc.) unchanged.

### Key Entities

- **NIfTI-Zarr Store**: A dataset stored in the Zarr chunked-array format
  that embeds NIfTI header metadata (orientation, voxel size, and related
  imaging parameters) alongside its chunked array data; may reside on the
  local filesystem.
- **Volume Header Metadata**: The imaging parameters (orientation matrix,
  voxel size, data type) already associated with every loaded volume in
  nitorch, regardless of source format.
- **Chunked Array**: The lazily evaluated, chunk-addressable representation
  of a nifti-zarr store's data, which can be partially read without loading
  the entire dataset into memory.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A user can load a nifti-zarr store and read its header
  metadata using the exact same call they already use for other supported
  volume formats, with no format-specific code required.
- **SC-002**: Reading a small sub-region of a nifti-zarr store whose full
  size exceeds available memory completes successfully, without requiring
  the full store to be read.
- **SC-003**: For a nifti-zarr store produced from a given NIfTI file, the
  orientation matrix, voxel size, and data type read back from the
  nifti-zarr store match those of the original NIfTI file exactly.
- **SC-004**: Attempting to load an invalid or incomplete nifti-zarr store
  produces a descriptive error in 100% of attempts, rather than a crash or
  silently incorrect data.

## Assumptions

- Only local-filesystem nifti-zarr stores are in scope; remote/cloud store
  access (e.g. object storage such as S3) is out of scope for this feature,
  consistent with this project's existing preference for no implicit
  network access.
- Only reading is in scope; writing/saving a nitorch volume out to a
  nifti-zarr store is out of scope for this feature.
- How a nifti-zarr store's multiple resolution levels (a multiscale pyramid)
  are exposed is an **open decision, deferred for discussion before
  planning** rather than assumed — see
  [multiscale-options.md](./multiscale-options.md) for the options
  considered (finest-level-by-default with opt-in access to others; or
  exposing all levels at once, mirroring nitorch's existing `ImagePyramid`
  concept). At minimum, the finest level MUST be exposed per FR-003/FR-004
  regardless of which option is chosen for the others.
- Loading a nifti-zarr store is offered as an additional, automatically
  recognized format alongside existing supported formats, rather than
  requiring the user to specify the format explicitly, consistent with how
  existing formats are already auto-detected.
