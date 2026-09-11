The two 32×32, 0.4-second green videos are generated test fixtures, without audio.
They exercise real WebEngine media decoding against the local test HTTP server.

```sh
ffmpeg -f lavfi -i color=c=green:s=32x32:d=0.4 -an -c:v libx264 -pix_fmt yuv420p browser-h264.mp4
ffmpeg -f lavfi -i color=c=green:s=32x32:d=0.4 -an -c:v libvpx-vp9 browser-vp9.webm
```

`preview.pdf` is a synthetic three-page document with selectable Chinese and English,
internal page links, and portrait/landscape pages. `preview-locked.pdf` contains the
same pages and has the test password `ava-test`. Both contain no private content.
Regenerate them with:

```sh
uv run --with reportlab --with pypdf python tests/fixtures/generate_pdf.py
```

The opt-in PDF benchmark generates its own 1,000-page, >1 MiB document in pytest's
temporary directory using Qt's QPdfWriter, with a reproducible image and Chinese text.

`oauth_server.py` runs a loopback OAuth authority and MCP peer with synthetic credentials.
It checks PKCE, one-time authorization codes, refresh-token rotation and protected tool calls.
The public-catalog mode reproduces Gmail's late authentication challenge without reading mail
or contacting Google. `test_mcp_oauth_sdk.py` adds small Google discovery fixtures (including
its issuer spelling and gzip responses) to keep compatibility checks deterministic.
