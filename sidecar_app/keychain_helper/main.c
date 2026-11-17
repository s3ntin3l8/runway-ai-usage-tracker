/* Byte-stable, read-only Keychain principal. No diagnostics or caller-defined native keys. */
#include <CoreFoundation/CoreFoundation.h>
#include <Security/Security.h>
#include <arpa/inet.h>
#include <errno.h>
#include <pwd.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define MAX_FRAME 4194304
#define TIMEOUT 30
#define MAX_REFERENCES 4096

static const char *SERVICES[] = {
    "Chrome Safe Storage",
    "Chromium Safe Storage",
    "Claude Code-credentials",
    "Claude Safe Storage",
    "Microsoft Edge Safe Storage",
    "gemini",
    "gh:github.com",
};

static CFMutableDictionaryRef references;
static bool interactive;

static bool is_type(CFTypeRef value, CFTypeID type) {
    return value && CFGetTypeID(value) == type;
}
static CFTypeRef get(CFDictionaryRef d, CFTypeRef key) {
    return CFDictionaryGetValue(d, key);
}
static CFMutableDictionaryRef dictionary(void) {
    return CFDictionaryCreateMutable(NULL, 0, &kCFTypeDictionaryKeyCallBacks,
                                    &kCFTypeDictionaryValueCallBacks);
}
static CFDictionaryRef error(const char *reason) {
    CFMutableDictionaryRef d = dictionary();
    CFStringRef s = CFStringCreateWithCString(NULL, reason, kCFStringEncodingUTF8);
    CFDictionarySetValue(d, CFSTR("error"), s);
    CFRelease(s);
    return d;
}
static CFDictionaryRef status_error(OSStatus status) {
    /* Every nonzero native status is a refusal, never evidence of a grant. */
    return error(status == errSecItemNotFound ? "missing" : "denied");
}
static CFDictionaryRef result(CFTypeRef value) {
    CFMutableDictionaryRef d = dictionary();
    CFDictionarySetValue(d, CFSTR("result"), value);
    return d;
}
static bool equals(CFTypeRef value, const char *text) {
    if (!is_type(value, CFStringGetTypeID())) return false;
    CFStringRef other = CFStringCreateWithCString(NULL, text, kCFStringEncodingUTF8);
    bool same = CFEqual(value, other);
    CFRelease(other);
    return same;
}
/* source_catalog.source_for_origin maps the CLI's directory-derived names to
 * the same Claude source. Admit only its exact eight-lowercase-hex derivation.
 */
static bool claude_service(CFTypeRef service) {
    if (equals(service, "Claude Code-credentials")) return true;
    if (!is_type(service, CFStringGetTypeID())) return false;
    const char *prefix = "Claude Code-credentials-";
    size_t length = strlen(prefix);
    char name[64];
    if (CFStringGetLength(service) != (CFIndex)(length + 8) ||
        !CFStringGetCString(service, name, sizeof(name), kCFStringEncodingUTF8) ||
        strlen(name) != length + 8 || strncmp(name, prefix, length)) return false;
    for (size_t i = length; i < length + 8; i++) {
        if (!((name[i] >= '0' && name[i] <= '9') || (name[i] >= 'a' && name[i] <= 'f'))) return false;
    }
    return true;
}
static bool service_allowed(CFTypeRef service) {
    for (size_t i = 0; i < sizeof(SERVICES) / sizeof(SERVICES[0]); i++) {
        if (equals(service, SERVICES[i])) return true;
    }
    return claude_service(service);
}
/* A selector may narrow only the catalog's account, never choose another account.
 * Services without an account in the catalog have no caller-specified selector.
 * Claude's selector is the OS login name (not an environment-controlled name).
 */
static bool account_allowed(CFStringRef service, CFTypeRef account) {
    if (equals(service, "gemini")) return equals(account, "antigravity");
    if (claude_service(service)) {
        struct passwd *user = getpwuid(getuid());
        return user && equals(account, user->pw_name);
    }
    return account == NULL;
}
static CFMutableDictionaryRef native_query(void) {
    CFMutableDictionaryRef q = dictionary();
    CFDictionarySetValue(q, kSecClass, kSecClassGenericPassword);
    CFDictionarySetValue(q, kSecUseDataProtectionKeychain, kCFBooleanFalse);
    CFDictionarySetValue(q, kSecReturnAttributes, kCFBooleanTrue);
    CFDictionarySetValue(q, kSecReturnPersistentRef, kCFBooleanTrue);
    return q;
}
static CFDictionaryRef clean_item(CFDictionaryRef item, bool with_data) {
    if (!is_type(item, CFDictionaryGetTypeID())) return NULL;
    CFTypeRef service = get(item, kSecAttrService), account = get(item, kSecAttrAccount);
    CFTypeRef ref = get(item, kSecValuePersistentRef), data = get(item, kSecValueData);
    if (!service_allowed(service) || !is_type(account, CFStringGetTypeID()) ||
        !is_type(ref, CFDataGetTypeID()) || CFDataGetLength(ref) == 0 ||
        (with_data && !is_type(data, CFDataGetTypeID()))) return NULL;
    if ((equals(service, "gemini") || claude_service(service)) &&
        !account_allowed(service, account)) return NULL;
    CFMutableDictionaryRef clean = dictionary();
    CFDictionarySetValue(clean, CFSTR("service"), service);
    CFDictionarySetValue(clean, CFSTR("account"), account);
    CFDictionarySetValue(clean, CFSTR("persistent"), ref);
    if (with_data) CFDictionarySetValue(clean, CFSTR("data"), data);
    return clean;
}
static CFDictionaryRef search(CFDictionaryRef query) {
    CFTypeRef service = get(query, CFSTR("service"));
    CFTypeRef account = get(query, CFSTR("account"));
    CFIndex count = CFDictionaryGetCount(query);
    if (!service_allowed(service) || count != (account ? 2 : 1) ||
        (account && !account_allowed(service, account))) return error("unavailable");
    CFMutableDictionaryRef q = native_query();
    CFDictionarySetValue(q, kSecAttrService, service);
    if (account) CFDictionarySetValue(q, kSecAttrAccount, account);
    else if (equals(service, "gemini")) CFDictionarySetValue(q, kSecAttrAccount, CFSTR("antigravity"));
    else if (claude_service(service)) {
        struct passwd *user = getpwuid(getuid());
        if (!user) { CFRelease(q); return error("unavailable"); }
        CFStringRef name = CFStringCreateWithCString(NULL, user->pw_name, kCFStringEncodingUTF8);
        CFDictionarySetValue(q, kSecAttrAccount, name);
        CFRelease(name);
    }
    CFDictionarySetValue(q, kSecMatchLimit, kSecMatchLimitAll);
    CFTypeRef found = NULL;
    OSStatus status = SecItemCopyMatching(q, &found);
    CFRelease(q);
    if (status) { if (found) CFRelease(found); return status_error(status); }
    if (!is_type(found, CFArrayGetTypeID())) {
        if (found) CFRelease(found);
        return error("unavailable");
    }
    CFMutableArrayRef items = CFArrayCreateMutable(NULL, 0, &kCFTypeArrayCallBacks);
    bool valid = true;
    for (CFIndex i = 0; i < CFArrayGetCount(found); i++) {
        CFDictionaryRef clean = clean_item(CFArrayGetValueAtIndex(found, i), false);
        if (!clean || !CFEqual(get(clean, CFSTR("service")), service) ||
            (account && !CFEqual(get(clean, CFSTR("account")), account)) ||
            CFDictionaryGetCount(references) >= MAX_REFERENCES) {
            if (clean) CFRelease(clean);
            valid = false; break;
        }
        CFArrayAppendValue(items, clean);
        CFRelease(clean);
    }
    CFRelease(found);
    if (valid && CFDictionaryGetCount(references) + CFArrayGetCount(items) > MAX_REFERENCES) valid = false;
    if (valid) {
        for (CFIndex i = 0; i < CFArrayGetCount(items); i++) {
            CFDictionaryRef item = CFArrayGetValueAtIndex(items, i);
            CFDictionarySetValue(references, get(item, CFSTR("persistent")), item);
        }
    }
    CFDictionaryRef response = valid ? result(items) : error("unavailable");
    CFRelease(items);
    return response;
}
static CFDictionaryRef read_reference(CFDataRef ref) {
    /* Only references issued by an allowlisted search in this process are capabilities.
     * A raw persistent reference from another process cannot bypass the allowlist.
     */
    if (!is_type(ref, CFDataGetTypeID())) return error("unavailable");
    CFDictionaryRef expected = get(references, ref);
    if (!expected) return error("unavailable");
    CFMutableDictionaryRef q = native_query();
    CFDictionarySetValue(q, kSecValuePersistentRef, ref);
    CFTypeRef found = NULL;
    OSStatus status = SecItemCopyMatching(q, &found);
    CFDictionaryRef metadata = status ? NULL : clean_item(found, false);
    if (found) CFRelease(found);
    if (!metadata || !CFEqual(metadata, expected)) {
        if (metadata) CFRelease(metadata);
        CFRelease(q);
        return status ? status_error(status) : error("unavailable");
    }
    CFRelease(metadata);
    CFDictionarySetValue(q, kSecReturnData, kCFBooleanTrue);
    found = NULL;
    if (interactive && SecKeychainSetUserInteractionAllowed(true)) {
        CFRelease(q); return error("unavailable");
    }
    status = SecItemCopyMatching(q, &found);
    OSStatus reset = SecKeychainSetUserInteractionAllowed(false);
    CFRelease(q);
    CFDictionaryRef clean = (status || reset) ? NULL : clean_item(found, true);
    if (found) CFRelease(found);
    if (!clean) return (status || reset) ? status_error(status ? status : reset) : error("unavailable");
    bool same = CFEqual(get(clean, CFSTR("persistent")), ref) &&
        CFEqual(get(clean, CFSTR("service")), get(expected, CFSTR("service"))) &&
        CFEqual(get(clean, CFSTR("account")), get(expected, CFSTR("account")));
    CFDictionaryRef response = same ? result(clean) : error("unavailable");
    CFRelease(clean);
    return response;
}
static CFDictionaryRef handle(CFTypeRef value) {
    if (!is_type(value, CFDictionaryGetTypeID()) || CFDictionaryGetCount(value) != 3 ||
        !equals(get(value, CFSTR("operation")), "copy")) return error("unavailable");
    CFTypeRef query = get(value, CFSTR("query")), values = get(value, CFSTR("values"));
    if (!is_type(query, CFDictionaryGetTypeID()) || !is_type(values, CFDictionaryGetTypeID()) ||
        CFDictionaryGetCount(values) != 0) return error("unavailable");
    CFTypeRef ref = get(query, CFSTR("persistent"));
    if (ref) return CFDictionaryGetCount(query) == 1 ? read_reference(ref) : error("unavailable");
    return search(query);
}
static bool read_exact(void *buffer, size_t size) {
    size_t offset = 0;
    while (offset < size) {
        ssize_t n = read(STDIN_FILENO, (char *)buffer + offset, size - offset);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return false;
        offset += (size_t)n;
    }
    return true;
}
static bool write_exact(const void *buffer, size_t size) {
    size_t offset = 0;
    while (offset < size) {
        ssize_t n = write(STDOUT_FILENO, (const char *)buffer + offset, size - offset);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return false;
        offset += (size_t)n;
    }
    return true;
}
int main(int argc, char **argv) {
    if (argc > 2 || (argc == 2 && strcmp(argv[1], "--interactive"))) return 1;
    interactive = argc == 2;
    signal(SIGPIPE, SIG_IGN);
    /* Default SIGALRM terminates stalled framing, Security calls and blocked writes. */
    if (SecKeychainSetUserInteractionAllowed(false)) return 1;
    references = dictionary();
    while (true) {
        uint32_t size;
        /* An idle reusable worker has no deadline; a partial frame does. */
        if (!read_exact(&size, 1)) break;
        alarm(TIMEOUT);
        if (!read_exact((char *)&size + 1, sizeof(size) - 1)) break;
        size = ntohl(size);
        if (!size || size > MAX_FRAME) break;
        UInt8 *buffer = malloc(size);
        if (!buffer) break;
        if (!read_exact(buffer, size)) { free(buffer); break; }
        /* The shared encoder emits binary plists only. Do not enable the legacy
         * OpenStep parser, which accepts non-plist text and can emit diagnostics.
         */
        if (size < 8 || memcmp(buffer, "bplist00", 8)) { free(buffer); break; }
        CFDataRef data = CFDataCreateWithBytesNoCopy(NULL, buffer, size, kCFAllocatorMalloc);
        CFPropertyListRef request = CFPropertyListCreateWithData(NULL, data, kCFPropertyListImmutable, NULL, NULL);
        CFRelease(data);
        if (!request) break;
        CFDictionaryRef response = handle(request);
        CFRelease(request);
        data = CFPropertyListCreateData(NULL, response, kCFPropertyListBinaryFormat_v1_0, 0, NULL);
        CFRelease(response);
        if (!data) break;
        CFIndex length = CFDataGetLength(data);
        bool valid = length > 0 && length <= MAX_FRAME;
        size = htonl((uint32_t)length);
        bool sent = valid && write_exact(&size, sizeof(size)) &&
            write_exact(CFDataGetBytePtr(data), (size_t)length);
        CFRelease(data);
        if (!sent) break;
        alarm(0);
    }
    CFRelease(references);
    return 0;
}
