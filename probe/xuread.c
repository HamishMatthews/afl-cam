// read-only survey of the camera's UVC extension-unit controls (GET_* only)
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/ioctl.h>
#include <linux/usb/video.h>
#include <linux/uvcvideo.h>
static int q(int fd, int unit, int sel, int query, unsigned char *buf, int len) {
    struct uvc_xu_control_query x = { .unit = unit, .selector = sel, .query = query, .size = len, .data = buf };
    return ioctl(fd, UVCIOC_CTRL_QUERY, &x);
}
static void hex(const char *tag, unsigned char *b, int n) {
    printf(" %s=", tag); for (int i = 0; i < n && i < 32; i++) printf("%02x", b[i]); if (n > 32) printf("..");
}
int main(int argc, char **argv) {
    int fd = open(argv[1], O_RDWR);
    if (fd < 0) { perror("open"); return 1; }
    int units[2] = {3, 4}, nsel[2] = {5, 25};
    for (int u = 0; u < 2; u++)
        for (int s = 1; s <= nsel[u] + 2; s++) {
            unsigned char info = 0, lenb[2] = {0};
            if (q(fd, units[u], s, UVC_GET_INFO, &info, 1) < 0) { printf("unit %d sel %2d: no\n", units[u], s); continue; }
            q(fd, units[u], s, UVC_GET_LEN, lenb, 2);
            int len = lenb[0] | lenb[1] << 8;
            printf("unit %d sel %2d: info=%02x(%s%s) len=%d", units[u], s, info, info & 1 ? "get" : "", info & 2 ? "+set" : "", len);
            if (len > 0 && len <= 512 && (info & 1)) {
                unsigned char *b = calloc(1, len);
                const char *tags[] = {"cur", "min", "max", "def", "res"};
                int qs[] = {UVC_GET_CUR, UVC_GET_MIN, UVC_GET_MAX, UVC_GET_DEF, UVC_GET_RES};
                for (int k = 0; k < 5; k++) { memset(b, 0, len); if (q(fd, units[u], s, qs[k], b, len) == 0) hex(tags[k], b, len); }
                free(b);
            }
            printf("\n");
        }
    return 0;
}
