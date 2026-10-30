"""Lab runs inside QEMU/KVM guests: distribution or source-built kernels, boot parameters, emulated
topology and host-side interference, with the host left unchanged.

Modules: `catalog` (kernels, published builds and matrices), `host` (the `schedlab vm` commands,
which drive podman), `machine` (inside the tools container: kernels, initramfs, QEMU), `build`
(inside the builder container: reproducible kernel and scheduler builds), `guest` (PID 1 inside the
VM), `initramfs` and `transport` (formats shared by both sides).
"""
