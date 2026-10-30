Kernels and sched_ext schedulers built by the lab, which `schedlab vm` downloads instead of building them. Each build is pinned by its SHA-256 in [`schedlab/vm/catalog.py`](https://github.com/naelolaiz/linux-scheduling-lab/blob/main/schedlab/vm/catalog.py) and was published only after a GitHub-hosted build reproduced that digest byte for byte.

- `linux-…-<digest>.tar.xz`: a kernel.org release built with the options its name spells: the kernel image, its config, the build record and any sched_ext example schedulers.
- `<distribution>-schedulers-<digest>.tar.xz`: the sched_ext example schedulers built for a distribution's kernel, and their record.
- `linux-*.tar.xz` without a digest: the kernel sources the builds used (kernel.org releases, and Rocky Linux's source tarball for its schedulers).
- `schedlab-<commit>.tar.gz`: the lab at a commit that published builds; its `vm/Containerfile` and `schedlab/vm/build.py` are the build recipe.

Check where an asset came from with `gh attestation verify FILE --repo naelolaiz/linux-scheduling-lab`, or rebuild it with `python3 -m schedlab vm verify NAME`. See [published builds](https://github.com/naelolaiz/linux-scheduling-lab/blob/main/docs/VIRTUAL_MACHINES.md#published-builds).
