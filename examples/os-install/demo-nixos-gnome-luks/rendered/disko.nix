# Rendered by prov (os-install, backend nixos-disko) from spec.toml; edit spec.toml and re-render.
# Partitioning (disko). WARNING: disko-install wipes this disk.
# Layout ext4-luks: ESP + LUKS2 partition -> LVM volume group pool. No key or password file is
# set, so disko asks for the new passphrase on the terminal when it formats the disk.
{
  disko.devices.disk.main = {
    type = "disk";
    device = "/dev/disk/by-id/nvme-EXAMPLE_SSD_SERIAL_0000000001";
    content = {
      type = "gpt";
      partitions = {
        ESP = {
          type = "EF00";
          size = "1G";
          content = {
            type = "filesystem";
            format = "vfat";
            mountpoint = "/boot";
            mountOptions = [ "umask=0077" ];
          };
        };
        luks = {
          size = "100%";
          content = {
            type = "luks";
            name = "crypted";
            settings.allowDiscards = true;
            content = { type = "lvm_pv"; vg = "pool"; };
          };
        };
      };
    };
  };
  disko.devices.lvm_vg.pool = {
    type = "lvm_vg";
    lvs = {
      swap = {
        size = "8G";
        content = { type = "swap"; resumeDevice = true; }; # boot.resumeDevice: resume from hibernation
      };
      root = {
        size = "64G";
        content = {
          type = "filesystem";
          format = "ext4";
          mountpoint = "/";
        };
      };
      data = {
        size = "200G";
        content = {
          type = "filesystem";
          format = "ext4";
          mountpoint = "/srv/data";
          mountOptions = [ "noatime" "nodev" "nosuid" "noexec" ];
        };
      };
      home = {
        size = "100%FREE";
        content = {
          type = "filesystem";
          format = "ext4";
          mountpoint = "/home";
        };
      };
    };
  };
}
