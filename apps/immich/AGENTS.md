# Immich archival-photo ordering methodology

This document records the investigation and matching methodology used for the
Immich archival-photo ordering problem. It is specific to this deployment and
should be updated if the storage layout or date-repair policy changes.

## Deployment context

- Immich runs in the `immich` Kubernetes namespace.
- The authoritative external photo tree is:
  `/filesystem/Media/Media/Photos`.
- The Kubernetes deployment mounts that tree read-only as `/external/photos`.
- The external photo tree is organized primarily into year directories and
  contains Synology `@eaDir` directories, which are excluded from analysis.
- Immich orders its timeline from media metadata, especially capture-date
  metadata. Folder names, filenames, and filesystem modification times are
  supporting evidence only.

## Google Takeout source

The useful Google Takeout archive is located at:

`/filesystem/Media/Google Takeouts/takeout-feb-2025/Takeout/Google Photos`

The archive contains matching `Photos from <year>` directories and Google
sidecar JSON files. The sidecars commonly contain:

```json
{
  "photoTakenTime": {
    "timestamp": "...",
    "formatted": "..."
  }
}
```

The timestamp is an epoch value represented by Google in UTC. It is converted
to `America/Phoenix` for comparison and reporting. The original capture
timezone may have been different, so the converted clock time is reference
information; the calendar date is the more reliable timeline signal.

## Investigation procedure

1. Scan the live photo tree with ExifTool for supported image files that lack
   `DateTimeOriginal`. Exclude `@eaDir` and do not modify files.
2. For each candidate, derive its year and filename, then search the matching
   Takeout year directory.
3. Compare SHA-256 hashes between the live image and Takeout images in the same
   filename family. Filename-family matching allows common Google/export
   variants such as `(1)`, `(2)`, `-edited`, or numeric suffixes, but the final
   match must be content-identical.
4. For each identical Takeout image, inspect matching JSON sidecars with `jq`
   and extract `photoTakenTime.timestamp`.
5. Convert each timestamp to `America/Phoenix` for the report and preserve the
   sidecar filename as evidence.
6. Exclude `@eaDir`, recycle-bin content, and unrelated Takeout products from
   the matching process.

## Classification rules

An entry is **confirmed** when the byte-identical Takeout image has exactly one
distinct `photoTakenTime` value. Multiple sidecars are acceptable when all of
them agree on the same timestamp.

An entry belongs in **potential/ambiguous** when any of the following applies:

- multiple distinct Takeout timestamps exist;
- the identical Takeout image exists but no usable `photoTakenTime` sidecar is
  present;
- no byte-identical Takeout image can be found;
- a date is inferred only from a filename; or
- another metadata field conflicts with the Takeout evidence.

Filename dates, such as `Screenshot_2017-04-09-22-04-39.png`, are useful clues
but are not treated as confirmed by themselves. Existing `CreateDate` or
`ModifyDate` values may be recorded as supporting evidence, but they do not
override conflicting Takeout sidecars automatically.

For the applied repair pass, a conflicting entry with a date-bearing filename
uses the filename date for `DateTimeOriginal`. Date-less and extension-only
candidates remain unresolved.

## Current reports

- [`MATCHES.md`](MATCHES.md) contains 60 confirmed matches.
- [`POTENTIAL.md`](POTENTIAL.md) contains 24 remaining ambiguous or incomplete
  matches and documents 3 applied filename-date corrections.
- Together with the 60 confirmed matches, these account for the 87 live image
  files found without `DateTimeOriginal` during this investigation.

The original evidence inventories were read-only. Three subsequent
`DateTimeOriginal` corrections are documented in `POTENTIAL.md`; no XMP
sidecars, Kubernetes manifests, or Immich database records were modified.

## Safety and next steps

- Do not apply a bulk EXIF correction from `POTENTIAL.md` without reviewing the
  conflicting candidates.
- Preserve a backup or verified copy before writing metadata to the
  authoritative photo tree.
- If metadata is corrected in the source tree, rescan the Immich external
  library afterward so Immich refreshes its asset metadata.
- Because the external library is mounted read-only, metadata edits made in
  the Immich UI cannot be persisted as external-file sidecars. Any durable
  correction must be made deliberately in the source tree or through a planned
  writable-sidecar workflow.
- The historical deployment notes referenced a missing Google Photos path under
  `/filesystem/Media/takeout-feb-2025`; the actual archive discovered during
  this investigation is under `/filesystem/Media/Google Takeouts/`. The
  archive is currently being used as a read-only evidence source, not mounted
  into Immich.
