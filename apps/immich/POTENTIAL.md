# Remaining potential or ambiguous Takeout matches

These files were not placed in `MATCHES.md` because the evidence is incomplete or conflicting.

- Dates shown are converted to `America/Phoenix` (MST) for reference; the original capture timezone may differ.
- A filename-derived date is only a clue; it is not treated as confirmed.
- For conflicting sidecars, every distinct candidate is listed.
- Potential Takeout source files are enumerated in the matching `Photos from <year>` directory by case-insensitive filename stem, ignoring the final image extension. `*_original` files are not collapsed into their base filename automatically.
- `Potential match count` counts candidate Takeout image files, not JSON sidecars. A filename-stem candidate is not confirmed unless the target is byte-identical and has usable matching sidecar evidence.
- The remaining-evidence table is read-only; the three applied metadata corrections are documented above.

## Applied corrections

Per the selected policy, `DateTimeOriginal` was written from the filename date for these three targets. Their conflicting sidecar dates remain preserved as evidence; the files remain outside `MATCHES.md`.

| Target file used by pod | `DateTimeOriginal` written | Reason |
|---|---|---|
| `/external/photos/2016/20160608_100906.jpg` | `2016-06-08 10:09:06 MST` | Filename date selected over the conflicting `00:09:06` sidecar value. |
| `/external/photos/2017/Screenshot_2017-04-09-22-04-39.png` | `2017-04-09 22:04:39 MST` | Filename date selected over the conflicting `2017-05-04 20:46:48 MST` sidecar value. The file content is JPEG despite its `.png` name. |
| `/external/photos/2018/Screenshot_2018-11-14-21-00-47.png` | `2018-11-14 21:00:47 MST` | Filename date selected over the conflicting `2018-12-11 11:58:09 MST` sidecar value. |

## Remaining entries

| Target file used by pod (`/external/photos`) | Potential Takeout source file(s) (relative to `Google Photos`) | Potential match count | Potential date(s) | Evidence / reason |
|---|---|---|---|---|
| `/external/photos/2017/Attachment_Original(1).png` | `Photos from 2017/Attachment_Original(1).png` | 1 | No date found | The source image is byte-identical, but no usable `photoTakenTime` sidecar was found. |
| `/external/photos/2017/Attachment_Original(2).png` | `Photos from 2017/Attachment_Original(2).png` | 1 | No date found | The source image is byte-identical, but no usable `photoTakenTime` sidecar was found. |
| `/external/photos/2017/Screenshot_2017-03-29-20-57-56-edited.png` | `Photos from 2017/Screenshot_2017-03-29-20-57-56-edited.png` | 1 | `2017-03-29 20:57:56 MST` (filename clue) | The source image is byte-identical, but no usable `photoTakenTime` sidecar was found. The date comes from the filename only. |
| `/external/photos/2017/Screenshot_2017-04-09-22-04-39-edited.png` | `Photos from 2017/Screenshot_2017-04-09-22-04-39-edited.png` | 1 | `2017-04-09 22:04:39 MST` (filename clue) | The source image is byte-identical, but no usable `photoTakenTime` sidecar was found. The date comes from the filename only. |
| `/external/photos/2018/Screenshot_2018-11-14-21-00-47-edited.png` | `Photos from 2018/Screenshot_2018-11-14-21-00-47-edited.png` | 1 | `2018-11-14 21:00:47 MST` (filename clue) | The source image is byte-identical, but no usable `photoTakenTime` sidecar was found. The date comes from the filename only. |
| `/external/photos/2019/IMG_0032(1).PNG` | `Photos from 2019/IMG_0032(1).PNG` | 1 | No date found | The source image is byte-identical, but no usable `photoTakenTime` sidecar was found. |
| `/external/photos/2019/IMG_0061(1).PNG` | `Photos from 2019/IMG_0061(1).PNG` | 1 | No date found | The source image is byte-identical, but no usable `photoTakenTime` sidecar was found. |
| `/external/photos/2019/IMG_0241(1).HEIC` | `Photos from 2019/IMG_0241(1).HEIC` | 1 | No date found | The source image is byte-identical, but no usable `photoTakenTime` sidecar was found. |
| `/external/photos/2019/IMG_0372(1).PNG` | `Photos from 2019/IMG_0372(1).PNG` | 1 | No date found | The source image is byte-identical, but no usable `photoTakenTime` sidecar was found. |
| `/external/photos/2019/IMG_1056(1).HEIC` | `Photos from 2019/IMG_1056(1).HEIC` | 1 | No date found | The source image is byte-identical, but no usable `photoTakenTime` sidecar was found. |
| `/external/photos/2019/IMG_1437(1).PNG` | `Photos from 2019/IMG_1437(1).PNG` | 1 | No date found | The source image is byte-identical, but no usable `photoTakenTime` sidecar was found. |
| `/external/photos/2019/IMG_2661(1).PNG` | `Photos from 2019/IMG_2661(1).PNG` | 1 | No date found | The source image is byte-identical, but no usable `photoTakenTime` sidecar was found. |
| `/external/photos/2021/cachedImage.PNG` | `Photos from 2021/cachedImage.PNG` | 1 | No date found | The source image is byte-identical, but no usable `photoTakenTime` sidecar was found. |
| `/external/photos/2021/cachedImage(1).PNG` | `Photos from 2021/cachedImage(1).PNG` | 1 | No date found | The source image is byte-identical, but no usable `photoTakenTime` sidecar was found. |
| `/external/photos/2021/image000000.png` | `Photos from 2021/image000000.png`<br>`Photos from 2021/image000000.jpg` | 2 | `2021-10-25 15:39:44 MST`<br>`2021-12-05 17:14:11 MST`<br>`2021-12-16 12:25:48 MST`<br>`2021-12-22 10:05:15 MST` | The PNG source is byte-identical and has conflicting sidecars. The JPEG is an additional same-stem extension candidate and is not treated as byte-identical to the PNG. Sidecar(s): image000000.png.supplemental-metadata(1).json; image000000.png.supplemental-metadata(2).json; image000000.png.supplemental-metadata(3).json; image000000.png.supplemental-metadata.json |
| `/external/photos/2021/image000000(2).png` | `Photos from 2021/image000000(2).png`<br>`Photos from 2021/image000000(2).jpg` | 2 | No date found | The PNG source is byte-identical, but no usable `photoTakenTime` sidecar was found. The JPEG is an additional same-stem extension candidate. |
| `/external/photos/2021/image000000(3).png` | `Photos from 2021/image000000(3).png`<br>`Photos from 2021/image000000(3).jpg` | 2 | No date found | The PNG source is byte-identical, but no usable `photoTakenTime` sidecar was found. The JPEG is an additional same-stem extension candidate. |
| `/external/photos/2021/image000000(4).png` | `Photos from 2021/image000000(4).png`<br>`Photos from 2021/image000000(4).jpg` | 2 | No date found | The PNG source is byte-identical, but no usable `photoTakenTime` sidecar was found. The JPEG is an additional same-stem extension candidate. |
| `/external/photos/2021/image000000(5).png` | `Photos from 2021/image000000(5).png`<br>`Photos from 2021/image000000(5).jpg` | 2 | No date found | The PNG source is byte-identical, but no usable `photoTakenTime` sidecar was found. The JPEG is an additional same-stem extension candidate. |
| `/external/photos/2021/image000001.png` | `Photos from 2021/image000001.png`<br>`Photos from 2021/image000001.jpg` | 2 | `2021-12-05 17:14:11 MST`<br>`2021-12-22 10:05:15 MST` | The PNG source is byte-identical and has conflicting sidecars. The JPEG is an additional same-stem extension candidate and is not treated as byte-identical to the PNG. Sidecar(s): image000001.png.supplemental-metadata(1).json; image000001.png.supplemental-metadata.json |
| `/external/photos/2021/image000001(1).png` | `Photos from 2021/image000001(1).png`<br>`Photos from 2021/image000001(1).jpg` | 2 | No date found | The PNG source is byte-identical, but no usable `photoTakenTime` sidecar was found. The JPEG is an additional same-stem extension candidate. |
| `/external/photos/2021/IMG_9194(1).PNG` | `Photos from 2021/IMG_9194(1).PNG` | 1 | No date found | The source image is byte-identical, but no usable `photoTakenTime` sidecar was found. |
| `/external/photos/2021/Screenshot_20211007-182725.png` | `Photos from 2021/Screenshot_20211007-182725.png` | 1 | `2021-10-07 18:27:25 MST` (filename clue) | The source image is byte-identical, but no usable `photoTakenTime` sidecar was found. The date comes from the filename only. |
| `/external/photos/2021/Screenshot_20211007-182735.png` | `Photos from 2021/Screenshot_20211007-182735.png` | 1 | `2021-10-07 18:27:35 MST` (filename clue) | The source image is byte-identical, but no usable `photoTakenTime` sidecar was found. The date comes from the filename only. |

Remaining potential/ambiguous target files: **24**. Remaining candidate Takeout image files listed: **31**.
