# Glaze

JSON utility dependency subset and MIT license from [Glaze v9.1.0](https://github.com/stephenberry/glaze/releases/tag/v9.1.0).
The 117 retained files are unmodified upstream files: `glaze/json.hpp`, every header
under `glaze/json/`, and their recursive local-include dependencies, including
conditional includes. All JSON utilities are retained for ongoing development.
Shared core, reflection, SIMD, and utility dependencies remain; `glaze/beve/header.hpp`
is directly required by core reflection. Other format implementations and networking
headers are omitted. Umbrella headers such as `glaze/glaze.hpp` are not provided.

No library build or download is required. When updating, extract the pinned source
archive and retain the same JSON utility include closure plus this license.

Source archive: `https://github.com/stephenberry/glaze/archive/refs/tags/v9.1.0.tar.gz`
SHA-256: `55f8a675397c505f9bee6c160e1c26b0085868f185d04d7a7727531fdbff4bd4`
