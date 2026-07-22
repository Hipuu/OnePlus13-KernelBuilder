#!/sbin/sh
### OnePlus 13 (dodge) AnyKernel3 install contract
## Repository-owned device logic; upstream tools/ak3-core.sh supplies primitives.

properties() { '
kernel.string=OnePlus 13 Kernel by Hipuu
do.devicecheck=1
do.modules=0
do.systemless=0
do.cleanup=1
do.cleanuponabort=0
device.name1=dodge
supported.versions=
supported.patchlevels=
supported.vendorpatchlevels=
'; }

# OnePlus 13 uses an A/B boot partition. Let AnyKernel3 resolve the active
# boot_a/boot_b node and preserve the existing ramdisk while replacing Image.
BLOCK=boot;
IS_SLOT_DEVICE=1;
RAMDISK_COMPRESSION=auto;
PATCH_VBMETA_FLAG=auto;

. tools/ak3-core.sh;

# Image-only package: no inherited reference-device ramdisk mutation.
split_boot;
flash_boot;
