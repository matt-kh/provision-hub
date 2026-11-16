# Rendered by prov (os-install, backend nixos-disko) from spec.toml; edit spec.toml and re-render.
# Partitioning (disko). WARNING: disko-install wipes this disk.
{
  disko.devices.disk.main = {
    type = "disk";
    device = "/dev/disk/by-id/nvme-EXAMPLE_SSD_SERIAL_0000000000";
    content = {
      type = "gpt";
      partitions = {
        ESP = {
          type = "EF00";
          size = "512M";
          content = {
            type = "filesystem";
            format = "vfat";
            mountpoint = "/boot";
            mountOptions = [ "umask=0077" ];
          };
        };
        swap = {
          size = "8G";
          content = { type = "swap"; };
        };
        root = {
          size = "100%";
          content = {
            type = "filesystem";
            format = "ext4";
            mountpoint = "/";
          };
        };
      };
    };
  };
}
