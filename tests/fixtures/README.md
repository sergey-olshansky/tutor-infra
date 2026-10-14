# Task #25 compiled CSS regression fixture

`task25-frontend.css` is the minified concatenation of
`lms/public/css/mytutor-common.css` and `frontend/src/styles/mytutor.css`
from LMS commit `d5f865044932a4ec7d4353f4b2b83346145e50d8`.
The latter's already-inlined `@import` was removed before minification.
Generated with the existing frontend dependency esbuild 0.19.12 using
`transformSync(source, {loader: 'css', minify: true})`.

This fixture exercises the inspector's complete frontend assertion set
against minified CSS, including the 48s variable-speed wave, 48s hue,
19s breathing animation and 12px decoration. Mutation tests retain
rejection of stale values, the extra painted layer and missing quiz JS.
Docker extraction is mocked; these tests never build, start or deploy an image.
The separate inspection workflow must still verify actual published bytes
against the exact source SHA and existing immutable image digest.
