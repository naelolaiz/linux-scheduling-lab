"""Lab runs inside QEMU/KVM guests: distribution or source-built kernels, boot parameters, emulated
topology and host-side interference, with the host left unchanged.

Modules: `catalog` (kernels and matrices), `host` (the `schedlab vm` commands, which drive podman),
`machine` (inside the tools container: kernels, initramfs, QEMU), `guest` (PID 1 inside the VM),
`initramfs` and `transport` (formats shared by both sides).
"""
