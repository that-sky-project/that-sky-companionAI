# PCRE2 and SLJIT notices

The source checkout includes `app/bin/libpcre2-8-0.dll`, used directly by
`app/bot/textcheck.py`. The binary contains the version string
`10.47 2025-10-21`. Its SHA-256 is:

`c135a87ed0f11eae8ffc4cb469671ff0b3f5d71fab5fb024e9b1e7241ca25b52`

`LICENCE.md` is copied verbatim from PCRE2's `pcre2-10.47` tag:
https://github.com/PCRE2Project/pcre2/blob/pcre2-10.47/LICENCE.md

The upstream license identifies PCRE2 as
`BSD-3-Clause WITH PCRE2-exception`, with separate BSD-2-Clause terms for
SLJIT. The tag records `deps/sljit` at commit
`45f910b78c6605ebf5b53d3ec7cb00f2312fe417`. Its full original license is
copied verbatim into `SLJIT-LICENSE`:
https://github.com/zherczeg/sljit/blob/45f910b78c6605ebf5b53d3ec7cb00f2312fe417/LICENSE

Original copyright holders and terms remain unchanged. These notices do not
relicense the library under this repository's project license.

The embedded version is evidence of the version claimed by this binary, not a
reproducible-build attestation. The original build toolchain, distributor and
compile options were not established in the retained source-export records.
The supplied license copies match that stated upstream version; no claim is
made that this DLL was rebuilt or byte-compared against an upstream artifact.
