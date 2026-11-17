/* Test-only replacements: this executable never calls the system Keychain. */
#include <CoreFoundation/CoreFoundation.h>
#include <Security/Security.h>
#include <stdio.h>
#include <pwd.h>
#include <unistd.h>
#include <stdlib.h>
#include <string.h>

static void record(const char *event) {
    FILE *file = fopen(getenv("RUNWAY_TEST_NATIVE_LOG"), "a");
    if (!file) abort();
    fprintf(file, "%s\n", event);
    fclose(file);
}

OSStatus RunwayTestInteraction(Boolean allowed) {
    record(allowed ? "interaction:on" : "interaction:off");
    return errSecSuccess;
}

OSStatus RunwayTestCopy(CFDictionaryRef query, CFTypeRef *output) {
    const char *scenario = getenv("RUNWAY_TEST_NATIVE_SCENARIO");
    bool search = CFDictionaryContainsKey(query, kSecAttrService);
    bool data = CFDictionaryContainsKey(query, kSecReturnData);
    record(search ? "search" : data ? "data" : "metadata");
    if (data && !strcmp(scenario, "deadline")) {
        unsigned int remaining = alarm(0);
        char event[32];
        snprintf(event, sizeof(event), "deadline:%u", remaining);
        record(event);
        alarm(remaining);
    }
    if (!CFEqual(CFDictionaryGetValue(query, kSecClass), kSecClassGenericPassword) ||
        CFDictionaryGetValue(query, kSecUseDataProtectionKeychain) != kCFBooleanFalse ||
        CFDictionaryGetValue(query, kSecReturnAttributes) != kCFBooleanTrue ||
        CFDictionaryGetValue(query, kSecReturnPersistentRef) != kCFBooleanTrue) abort();
    if ((search && !strcmp(scenario, "search-denied")) ||
        (!search && !data && !strcmp(scenario, "metadata-denied")) ||
        (data && !strcmp(scenario, "data-denied"))) return -25293;
    if (data && !strcmp(scenario, "data-interaction-denied")) return -25308;
    CFMutableDictionaryRef item = CFDictionaryCreateMutable(NULL, 0,
        &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
    bool changed_service = (!search && !data && !strcmp(scenario, "metadata-service")) ||
        (data && !strcmp(scenario, "data-service"));
    bool changed_account = (!search && !data && !strcmp(scenario, "metadata-account")) ||
        (data && !strcmp(scenario, "data-account"));
    static CFStringRef selected_service;
    if (!strcmp(scenario, "suffix") && search) {
        selected_service = CFRetain(CFDictionaryGetValue(query, kSecAttrService));
        if (!CFDictionaryContainsKey(query, kSecAttrAccount)) abort();
    }
    CFDictionarySetValue(item, kSecAttrService,
        changed_service ? CFSTR("Microsoft Edge Safe Storage") : CFSTR("Chrome Safe Storage"));
    CFDictionarySetValue(item, kSecAttrAccount,
        changed_account ? CFSTR("changed-synthetic-account") : CFSTR("synthetic-account"));
    if (!strcmp(scenario, "suffix")) {
        CFStringRef account = CFStringCreateWithCString(NULL, getpwuid(getuid())->pw_name,
            kCFStringEncodingUTF8);
        CFDictionarySetValue(item, kSecAttrService, selected_service);
        CFDictionarySetValue(item, kSecAttrAccount, account);
        CFRelease(account);
    }
    CFDataRef reference = CFDataCreate(NULL, (const UInt8 *)"ref", 3);
    CFDictionarySetValue(item, kSecValuePersistentRef, reference);
    CFRelease(reference);
    if (data) {
        CFDataRef value = CFDataCreate(NULL, (const UInt8 *)"synthetic-secret", 16);
        CFDictionarySetValue(item, kSecValueData, value);
        CFRelease(value);
    }
    if (search) {
        const void *items[] = {item};
        if (!strcmp(scenario, "partial-search")) {
            CFMutableDictionaryRef invalid = CFDictionaryCreateMutableCopy(NULL, 0, item);
            CFDictionarySetValue(invalid, kSecAttrService, CFSTR("forbidden-service"));
            const void *partial_items[] = {item, invalid};
            *output = CFArrayCreate(NULL, partial_items, 2, &kCFTypeArrayCallBacks);
            CFRelease(invalid);
        } else *output = CFArrayCreate(NULL, items, 1, &kCFTypeArrayCallBacks);
        CFRelease(item);
    } else *output = item;
    return errSecSuccess;
}
