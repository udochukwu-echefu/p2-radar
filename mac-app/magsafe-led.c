// magsafe-led: control the MagSafe 3 charging LED on Apple Silicon MacBooks.
//
// The LED is driven by the SMC key "ACLC" (AC LED Color):
//   0x00 = system default (macOS decides green/orange)
//   0x01 = off
//   0x03 = green
//   0x04 = orange
//
// Reading needs no privileges; writing needs root.
// macOS resets the LED when the cable is replugged or after sleep, so
// `magsafe-led daemon off` keeps re-applying the chosen state.
//
// Build: clang -O2 -framework IOKit -framework CoreFoundation -o magsafe-led magsafe-led.c

#include <IOKit/IOKitLib.h>
#include <IOKit/ps/IOPowerSources.h>
#include <IOKit/pwr_mgt/IOPMLib.h>
#include <IOKit/IOMessage.h>
#include <CoreFoundation/CoreFoundation.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define KERNEL_INDEX_SMC 2
#define SMC_CMD_READ_BYTES 5
#define SMC_CMD_WRITE_BYTES 6
#define SMC_CMD_READ_KEYINFO 9

typedef struct {
    char major, minor, build, reserved[1];
    UInt16 release;
} SMCVers;

typedef struct {
    UInt16 version, length;
    UInt32 cpuPLimit, gpuPLimit, memPLimit;
} SMCPLimit;

typedef struct {
    UInt32 dataSize;
    UInt32 dataType;
    char dataAttributes;
} SMCKeyInfo;

typedef struct {
    UInt32 key;
    SMCVers vers;
    SMCPLimit pLimitData;
    SMCKeyInfo keyInfo;
    char result, status, data8;
    UInt32 data32;
    unsigned char bytes[32];
} SMCKeyData;

_Static_assert(sizeof(SMCKeyData) == 80, "SMCKeyData must be 80 bytes");

static io_connect_t conn = 0;

static UInt32 fourcc(const char *s) {
    return ((UInt32)s[0] << 24) | ((UInt32)s[1] << 16) | ((UInt32)s[2] << 8) | (UInt32)s[3];
}

static int smc_open(void) {
    io_service_t svc = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("AppleSMC"));
    if (!svc) { fprintf(stderr, "AppleSMC service not found\n"); return -1; }
    kern_return_t kr = IOServiceOpen(svc, mach_task_self(), 0, &conn);
    IOObjectRelease(svc);
    if (kr != KERN_SUCCESS) { fprintf(stderr, "IOServiceOpen failed: 0x%x\n", kr); return -1; }
    return 0;
}

static kern_return_t smc_call(SMCKeyData *in, SMCKeyData *out) {
    size_t outSize = sizeof(SMCKeyData);
    return IOConnectCallStructMethod(conn, KERNEL_INDEX_SMC, in, sizeof(SMCKeyData), out, &outSize);
}

static int smc_keyinfo(const char *key, SMCKeyInfo *info) {
    SMCKeyData in = {0}, out = {0};
    in.key = fourcc(key);
    in.data8 = SMC_CMD_READ_KEYINFO;
    kern_return_t kr = smc_call(&in, &out);
    if (kr != KERN_SUCCESS || out.result != 0) return -1;
    *info = out.keyInfo;
    return 0;
}

static int smc_read_u8(const char *key, unsigned char *val) {
    SMCKeyInfo info;
    if (smc_keyinfo(key, &info) != 0) return -1;
    SMCKeyData in = {0}, out = {0};
    in.key = fourcc(key);
    in.keyInfo.dataSize = info.dataSize;
    in.data8 = SMC_CMD_READ_BYTES;
    kern_return_t kr = smc_call(&in, &out);
    if (kr != KERN_SUCCESS || out.result != 0) return -1;
    *val = out.bytes[0];
    return 0;
}

static int smc_write_u8(const char *key, unsigned char val) {
    SMCKeyInfo info;
    if (smc_keyinfo(key, &info) != 0) return -1;
    SMCKeyData in = {0}, out = {0};
    in.key = fourcc(key);
    in.keyInfo.dataSize = info.dataSize;
    in.data8 = SMC_CMD_WRITE_BYTES;
    in.bytes[0] = val;
    kern_return_t kr = smc_call(&in, &out);
    if (kr != KERN_SUCCESS) {
        if (kr == kIOReturnNotPrivileged) fprintf(stderr, "Writing the LED needs root: run with sudo\n");
        else fprintf(stderr, "SMC write failed: 0x%x\n", kr);
        return -1;
    }
    if (out.result != 0) { fprintf(stderr, "SMC write rejected (result %d)\n", out.result); return -1; }
    return 0;
}

static const char *name_for(unsigned char v) {
    switch (v) {
        case 0x00: return "system";
        case 0x01: return "off";
        case 0x03: return "green";
        case 0x04: return "orange";
        default:   return "other";
    }
}

static int value_for(const char *name) {
    if (!strcmp(name, "off"))    return 0x01;
    if (!strcmp(name, "on") || !strcmp(name, "system") || !strcmp(name, "auto")) return 0x00;
    if (!strcmp(name, "green"))  return 0x03;
    if (!strcmp(name, "orange")) return 0x04;
    return -1;
}

// ---- daemon mode ----

static unsigned char target;
static int target_is_system = 0;
static const char *config_path = NULL;
static io_connect_t pm_root = 0;

// When a config file is given, re-read the wanted mode from it on every check.
// The file holds one word: off | green | orange | system. Missing/unreadable -> keep last mode.
static void reload_config(void) {
    if (!config_path) return;
    FILE *f = fopen(config_path, "r");
    if (!f) return;
    char buf[32] = {0};
    if (fgets(buf, sizeof buf, f)) {
        buf[strcspn(buf, " \t\r\n")] = 0;
        int v = value_for(buf);
        if (v >= 0) {
            int sys = (v == 0x00);
            if ((unsigned char)v != target || sys != target_is_system)
                fprintf(stderr, "magsafe-led: mode is now %s\n", name_for((unsigned char)v));
            target = (unsigned char)v;
            target_is_system = sys;
        }
    }
    fclose(f);
}

static unsigned char last_written = 0xFF;

static void enforce(const char *why) {
    reload_config();
    if (target_is_system) {
        // Hand control back to macOS once, then leave the LED alone.
        if (last_written != 0x00 && smc_write_u8("ACLC", 0x00) == 0) {
            last_written = 0x00;
            fprintf(stderr, "magsafe-led: LED back under macOS control (%s)\n", why);
        }
        return;
    }
    unsigned char cur;
    if (smc_read_u8("ACLC", &cur) == 0 && cur == target) return;
    if (smc_write_u8("ACLC", target) == 0) {
        last_written = target;
        fprintf(stderr, "magsafe-led: set LED to %s (%s)\n", name_for(target), why);
    }
}

static void power_changed(void *ctx) { (void)ctx; enforce("power source changed"); }

static void timer_fired(CFRunLoopTimerRef t, void *ctx) { (void)t; (void)ctx; enforce("periodic check"); }

static void sleep_wake(void *ctx, io_service_t svc, natural_t type, void *arg) {
    (void)ctx; (void)svc;
    switch (type) {
        case kIOMessageCanSystemSleep:
        case kIOMessageSystemWillSleep:
            IOAllowPowerChange(pm_root, (long)arg);
            break;
        case kIOMessageSystemHasPoweredOn:
            enforce("wake");
            break;
    }
}

static int run_daemon(void) {
    if (geteuid() != 0) { fprintf(stderr, "daemon mode needs root\n"); return 1; }
    setvbuf(stderr, NULL, _IOLBF, 0);
    enforce("start");

    CFRunLoopSourceRef ps = IOPSNotificationCreateRunLoopSource(power_changed, NULL);
    if (ps) CFRunLoopAddSource(CFRunLoopGetCurrent(), ps, kCFRunLoopDefaultMode);

    IONotificationPortRef port;
    io_object_t notifier;
    pm_root = IORegisterForSystemPower(NULL, &port, sleep_wake, &notifier);
    if (pm_root)
        CFRunLoopAddSource(CFRunLoopGetCurrent(), IONotificationPortGetRunLoopSource(port), kCFRunLoopDefaultMode);

    // Safety net: macOS can flip the LED back on its own (e.g. when charging state changes).
    CFRunLoopTimerRef timer = CFRunLoopTimerCreate(NULL, CFAbsoluteTimeGetCurrent() + 5, 5, 0, 0, timer_fired, NULL);
    CFRunLoopAddTimer(CFRunLoopGetCurrent(), timer, kCFRunLoopDefaultMode);

    CFRunLoopRun();
    return 0;
}

static void usage(void) {
    fprintf(stderr,
        "usage: magsafe-led status\n"
        "       sudo magsafe-led off | on | green | orange\n"
        "       sudo magsafe-led daemon off|green|orange   (keep re-applying)\n"
        "       sudo magsafe-led daemon --config <file>    (mode read from file, re-read every 5s)\n");
}

int main(int argc, char **argv) {
    if (argc < 2) { usage(); return 1; }
    if (smc_open() != 0) return 1;

    if (!strcmp(argv[1], "status")) {
        unsigned char v;
        if (smc_read_u8("ACLC", &v) != 0) {
            fprintf(stderr, "Could not read SMC key ACLC (no MagSafe 3 LED on this Mac?)\n");
            return 1;
        }
        printf("%s (0x%02x)\n", name_for(v), v);
        return 0;
    }

    if (!strcmp(argv[1], "daemon")) {
        target = 0x01;
        if (argc > 3 && !strcmp(argv[2], "--config")) {
            config_path = argv[3];
        } else if (argc > 2) {
            int v = value_for(argv[2]);
            if (v < 0) { usage(); return 1; }
            target = (unsigned char)v;
            target_is_system = (v == 0x00);
        }
        return run_daemon();
    }

    int v = value_for(argv[1]);
    if (v < 0) { usage(); return 1; }
    if (smc_write_u8("ACLC", (unsigned char)v) != 0) return 1;
    printf("LED set to %s\n", name_for((unsigned char)v));
    return 0;
}
