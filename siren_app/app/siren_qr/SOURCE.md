# siren_qr (vendored)

Copied unchanged from https://github.com/thaniyuck/siren, folder `encoder/siren_qr/`,
commit `93f7359` (22 Nov 2026). The viewer that opens the QR link lives in the same
repository (`docs/`) and is published at https://thaniyuck.github.io/siren/.

- Do not edit these files here. Change them upstream, then copy them again, so the
  encoder and the viewer's decoder (`docs/decode.js`) stay in step.
- `tests/qr_viewer/decode.js` is a copy of the viewer's decoder from the same commit.
  `tests/test_transport_qr.py` decodes our labels with it, so a mismatch fails the tests.
- SIRENweb builds the input record in `app/services/transport_record.py`.
