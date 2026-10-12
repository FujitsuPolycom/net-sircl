#define _GNU_SOURCE
#include <sircl_core/core.h>
#include <stdio.h>
#include <string.h>
#include <dirent.h>
/* Extracted from net-sircl 7b81b968; root names /sys/class/infiniband or a test fixture. */
uint64_t sircl_core_sysfs_guid(const char *root, const char *name) {
  char path[256], text[64] = {0};
  snprintf(path, sizeof path, "%s/%s/sys_image_guid", root, name);
  FILE *f = fopen(path, "r");
  if (!f) return 0;
  if (!fgets(text, sizeof text, f)) text[0] = '\0';
  fclose(f);
  uint64_t v = 0;
  for (char *c = text; *c; ++c) {
    int n = (*c >= '0' && *c <= '9') ? *c - '0' : (*c >= 'a' && *c <= 'f') ? *c - 'a' + 10 : -1;
    if (n >= 0) v = v << 4 | (uint64_t)n;
  }
  return v;
}

void sircl_core_sysfs_netdev(const char *root, const char *name, char *out, size_t len) {
  if (!out || !len) return;
  char path[256];
  snprintf(path, sizeof path, "%s/%s/device/net", root, name);
  out[0] = '\0';
  DIR *dir = opendir(path);
  if (!dir) return;
  for (struct dirent *e; (e = readdir(dir));)
    if (e->d_name[0] != '.' && strlen(e->d_name) < len) {
      memcpy(out, e->d_name, strlen(e->d_name) + 1);
      break;
    }
  closedir(dir);
}

/* The GID index of the function's RoCE v2 IPv4 address: the lowest index whose type is "RoCE v2" and whose
 * GID is IPv4-mapped (as NCCL's IB transport prefers by default, net_ib/connect.cc:263-291). -1: none. */
int sircl_core_gid_index(const char *root, const char *name, unsigned port) {
  for (int i = 0; i < 256; ++i) {
    char path[256], text[128] = {0};
    snprintf(path, sizeof path, "%s/%s/ports/%u/gid_attrs/types/%d", root, name, port, i);
    FILE *f = fopen(path, "r");
    if (!f) continue;
    int ok = fgets(text, sizeof text, f) && !strncmp(text, "RoCE v2", 7);
    fclose(f);
    if (!ok) continue;
    snprintf(path, sizeof path, "%s/%s/ports/%u/gids/%d", root, name, port, i);
    f = fopen(path, "r");
    if (!f) continue;
    ok = fgets(text, sizeof text, f) && !strncmp(text, "0000:0000:0000:0000:0000:ffff:", 30) &&
         strncmp(text + 30, "0000:0000", 9);
    fclose(f);
    if (ok) return i;
  }
  return -1;
}