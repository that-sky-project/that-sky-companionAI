# Third-party software and format research

Runtime dependencies are pinned in `engine/package-lock.json`. Their original licenses remain in their packages:

- Rapier 3D compatibility package 0.21.0 — Apache-2.0, https://github.com/dimforge/rapier.js
- Recast Navigation JS 0.44.0 (core/generators/WASM) — MIT, https://github.com/isaac-mason/recast-navigation-js
- meshoptimizer 1.3.0 official WASM decoder — MIT, https://github.com/zeux/meshoptimizer
- NumPy — BSD-3-Clause, https://github.com/numpy/numpy

`engine/vendor/tile_generator.mjs` retains the MIT 0.44.0 per-tile generator with native allocation cleanup, explicit Detour layer identity, and bounded expandable chunk-query capacity added. The complete original notice is in `engine/vendor/LICENSE.recast-generators`; the rasterization algorithm is otherwise unchanged. The installed dependency files themselves are not patched.

Format research used pinned source snapshots with their original license files retained separately outside this source checkout. No `research` directory is included here:

- SkyVEx — https://github.com/lingyunalingyun/SkyVEx (main project MIT; individual bundled modules have separate licenses).
- SkyEngineTools — https://github.com/oldmud0/SkyEngineTools (ISC).
- that-sky-level-meshes — https://github.com/that-sky-project/that-sky-level-meshes (MIT).
- that-sky-compiler — https://github.com/that-sky-project/that-sky-compiler (LGPL-2.1/SLDL as recorded in the source manifest).
- that-sky-level — https://github.com/that-sky-project/that-sky-level (AGPL-3.0).

The independent readers preserve unknown fields and do not execute upstream tools or compiled game scripts. The LGPL `meshes2obj_json.py` was used as a research comparison oracle, not shipped as a runtime decoder. Runtime meshoptimizer decoding uses the official MIT package instead. No unlicensed `infdev` code is copied into runtime modules.

Game resources are not part of the source distribution. Local output assets are derived from the APK explicitly supplied by the user and retain their original ownership. No account, server credential, or private configuration belongs in an engine source package.
