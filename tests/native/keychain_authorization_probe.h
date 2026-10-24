/* Public metadata APIs only. Included solely in the quarantined synthetic tool.
 * Standalone syntax check (static helpers are intentionally unused in this form):
 * clang -x c -fsyntax-only -Wall -Wextra -Werror -Wno-unused-function \
 *   -Wno-deprecated-declarations tests/native/keychain_authorization_probe.h
 */
#include <CoreFoundation/CoreFoundation.h>
#include <Security/Security.h>
#include <libproc.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

static const unsigned char authorization_original[] = "synthetic-public-update-original";

static CFStringRef identity_partition(SecCodeRef code) {
    CFDictionaryRef info = NULL;
    CFStringRef result = NULL;
    if (!code || SecCodeCopySigningInformation((SecStaticCodeRef)code, kSecCSSigningInformation, &info)) return NULL;
    CFTypeRef team = CFDictionaryGetValue(info, kSecCodeInfoTeamIdentifier);
    CFTypeRef hash = CFDictionaryGetValue(info, kSecCodeInfoUnique);
    if (team && CFGetTypeID(team) == CFStringGetTypeID() && CFStringGetLength(team) > 0) {
        result = CFStringCreateWithFormat(NULL, NULL, CFSTR("teamid:%@"), team);
    } else if (hash && CFGetTypeID(hash) == CFDataGetTypeID() && CFDataGetLength(hash) > 0) {
        CFMutableStringRef value = CFStringCreateMutable(NULL, 0);
        CFStringAppend(value, CFSTR("cdhash:"));
        for (CFIndex i = 0; i < CFDataGetLength(hash); i++)
            CFStringAppendFormat(value, NULL, CFSTR("%02x"), CFDataGetBytePtr(hash)[i]);
        result = value;
    }
    CFRelease(info);
    return result;
}

static CFArrayRef partition_values(CFStringRef description) {
    if (!description || CFGetTypeID(description) != CFStringGetTypeID()) return NULL;
    CFIndex size = CFStringGetLength(description);
    if (!size || size % 2 || size > 1048576) return NULL;
    unsigned char *bytes = malloc((size_t)size / 2);
    if (!bytes) return NULL;
    int valid = 1;
    for (CFIndex i = 0; i < size; i += 2) {
        unsigned int pair = 0;
        for (CFIndex j = 0; j < 2; j++) {
            UniChar c = CFStringGetCharacterAtIndex(description, i + j);
            unsigned int digit;
            if (c >= '0' && c <= '9') digit = c - '0';
            else if (c >= 'a' && c <= 'f') digit = c - 'a' + 10;
            else if (c >= 'A' && c <= 'F') digit = c - 'A' + 10;
            else { valid = 0; break; }
            pair = pair * 16 + digit;
        }
        if (!valid) break;
        bytes[i / 2] = (unsigned char)pair;
    }
    CFDataRef data = valid ? CFDataCreate(NULL, bytes, size / 2) : NULL;
    free(bytes);
    if (!data) return NULL;
    CFPropertyListRef plist = CFPropertyListCreateWithData(NULL, data, kCFPropertyListImmutable, NULL, NULL);
    CFRelease(data);
    CFArrayRef result = NULL;
    if (plist && CFGetTypeID(plist) == CFDictionaryGetTypeID() && CFDictionaryGetCount(plist) == 1) {
        CFTypeRef values = CFDictionaryGetValue(plist, CFSTR("Partitions"));
        if (values && CFGetTypeID(values) == CFArrayGetTypeID()) {
            valid = 1;
            for (CFIndex i = 0; i < CFArrayGetCount(values); i++)
                if (CFGetTypeID(CFArrayGetValueAtIndex(values, i)) != CFStringGetTypeID()) valid = 0;
            if (valid) result = CFRetain(values);
        }
    }
    if (plist) CFRelease(plist);
    return result;
}

static const char *maybe_bool(int known, int value) {
    return !known ? "null" : value ? "true" : "false";
}

static OSStatus authorization_metadata(SecKeychainItemRef item) {
    SecCodeRef self = NULL, creator = NULL;
    SecTrustedApplicationRef self_app = NULL, creator_app = NULL;
    CFDataRef self_blob = NULL, creator_blob = NULL;
    CFStringRef self_id = NULL, creator_id = NULL;
    SecAccessRef access = NULL;
    CFArrayRef acls = NULL;
    char parent_path[PROC_PIDPATHINFO_MAXSIZE];
    pid_t pid = getppid();
    CFNumberRef number = CFNumberCreate(NULL, kCFNumberIntType, &pid);
    const void *key = kSecGuestAttributePid, *value = number;
    CFDictionaryRef attrs = CFDictionaryCreate(NULL, &key, &value, 1,
        &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
    if (!SecCodeCopySelf(kSecCSDefaultFlags, &self)) self_id = identity_partition(self);
    if (!SecCodeCopyGuestWithAttributes(NULL, attrs, kSecCSDefaultFlags, &creator)) creator_id = identity_partition(creator);
    if (!SecTrustedApplicationCreateFromPath(NULL, &self_app))
        (void)SecTrustedApplicationCopyData(self_app, &self_blob);
    if (proc_pidpath(pid, parent_path, sizeof(parent_path)) > 0 &&
        !SecTrustedApplicationCreateFromPath(parent_path, &creator_app))
        (void)SecTrustedApplicationCopyData(creator_app, &creator_blob);
    int ordinary = 0, self_trust = 0, creator_trust = 0, broad = 0;
    int partitions = 0, partition_ok = 1, self_partition = 0, creator_partition = 0;
    OSStatus status = SecKeychainItemCopyAccess(item, &access);
    if (!status) status = SecAccessCopyACLList(access, &acls);
    if (!status) for (CFIndex i = 0; i < CFArrayGetCount(acls); i++) {
        SecACLRef acl = (SecACLRef)CFArrayGetValueAtIndex(acls, i);
        CSSM_ACL_AUTHORIZATION_TAG tags[64];
        uint32 count = 64;
        CFArrayRef apps = NULL;
        CFStringRef description = NULL;
        SecKeychainPromptSelector selector;
        status = SecACLGetAuthorizations(acl, tags, &count);
        if (status || count > 64) { if (!status) status = errSecParam; break; }
        int decrypt = 0, partition = 0;
        for (uint32 j = 0; j < count; j++) {
            decrypt |= tags[j] == CSSM_ACL_AUTHORIZATION_DECRYPT;
            partition |= tags[j] == 65538;
        }
        if (!decrypt && !partition) continue;
        status = SecACLCopyContents(acl, &apps, &description, &selector);
        if (status) break;
        if (decrypt) {
            ordinary = 1;
            broad |= apps == NULL;
            if (apps) for (CFIndex j = 0; j < CFArrayGetCount(apps); j++) {
                CFDataRef blob = NULL;
                OSStatus copied = SecTrustedApplicationCopyData((SecTrustedApplicationRef)CFArrayGetValueAtIndex(apps, j), &blob);
                if (copied) { status = copied; break; }
                self_trust |= self_blob && CFEqual(blob, self_blob);
                creator_trust |= creator_blob && CFEqual(blob, creator_blob);
                CFRelease(blob);
            }
        }
        if (partition) {
            partitions = 1;
            CFArrayRef values = partition_values(description);
            if (!values) partition_ok = 0;
            else {
                CFRange range = CFRangeMake(0, CFArrayGetCount(values));
                self_partition |= self_id && CFArrayContainsValue(values, range, self_id);
                creator_partition |= creator_id && CFArrayContainsValue(values, range, creator_id);
                CFRelease(values);
            }
        }
        if (description) CFRelease(description);
        if (apps) CFRelease(apps);
        if (status) break;
    }
    int trust_known = !status && ordinary && self_blob && creator_blob;
    int partition_known = !status && partitions && partition_ok;
    printf("\"authorization\":{\"metadata_status\":%d,\"python_reader_identity_available\":%s,\"helper_identity_available\":%s,"
        "\"identity_partitions_equal\":%s,\"trust_comparison_available\":%s,\"python_reader_trust_blob_equal\":%s,"
        "\"helper_trust_blob_equal\":%s,\"broad_decrypt_access\":%s,\"partition_acl_present\":%s,"
        "\"partition_metadata_available\":%s,\"partition_contains_python_reader_identity\":%s,\"partition_contains_helper_identity\":%s}",
        (int)status, maybe_bool(1, creator_id != NULL), maybe_bool(1, self_id != NULL),
        maybe_bool(self_id && creator_id, self_id && creator_id && CFEqual(self_id, creator_id)),
        maybe_bool(1, trust_known), maybe_bool(trust_known, creator_trust), maybe_bool(trust_known, self_trust),
        maybe_bool(!status && ordinary, broad), maybe_bool(!status, partitions), maybe_bool(1, partition_known),
        maybe_bool(partition_known && creator_id, creator_partition), maybe_bool(partition_known && self_id, self_partition));
    if (acls) CFRelease(acls);
    if (access) CFRelease(access);
    if (self_id) CFRelease(self_id);
    if (creator_id) CFRelease(creator_id);
    if (self_blob) CFRelease(self_blob);
    if (creator_blob) CFRelease(creator_blob);
    if (self_app) CFRelease(self_app);
    if (creator_app) CFRelease(creator_app);
    if (self) CFRelease(self);
    if (creator) CFRelease(creator);
    CFRelease(attrs);
    CFRelease(number);
    return status;
}

static OSStatus seed_helper_item(SecKeychainRef keychain) {
    char parent_path[PROC_PIDPATHINFO_MAXSIZE];
    SecTrustedApplicationRef self = NULL, reader = NULL;
    SecAccessRef access = NULL;
    CFArrayRef apps = NULL;
    CFDataRef data = NULL;
    CFMutableDictionaryRef query = NULL;
    OSStatus status = errSecParam;
    if (proc_pidpath(getppid(), parent_path, sizeof(parent_path)) <= 0) goto done;
    status = SecTrustedApplicationCreateFromPath(NULL, &self);
    if (status) goto done;
    status = SecTrustedApplicationCreateFromPath(parent_path, &reader);
    if (status) goto done;
    const void *trusted[] = { self, reader };
    apps = CFArrayCreate(NULL, trusted, 2, &kCFTypeArrayCallBacks);
    status = SecAccessCreate(CFSTR("Runway isolated synthetic acceptance"), apps, &access);
    if (status) goto done;
    query = CFDictionaryCreateMutable(NULL, 0, &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
    data = CFDataCreate(NULL, authorization_original, sizeof(authorization_original) - 1);
    CFDictionarySetValue(query, kSecClass, kSecClassGenericPassword);
    CFDictionarySetValue(query, kSecAttrService, CFSTR("Runway synthetic public-update probe"));
    CFDictionarySetValue(query, kSecAttrAccount, CFSTR("runway-synthetic-acceptance"));
    CFDictionarySetValue(query, kSecUseKeychain, keychain);
    CFDictionarySetValue(query, kSecUseDataProtectionKeychain, kCFBooleanFalse);
    CFDictionarySetValue(query, kSecAttrAccess, access);
    CFDictionarySetValue(query, kSecValueData, data);
    status = SecItemAdd(query, NULL);
done:
    if (query) CFRelease(query);
    if (data) CFRelease(data);
    if (access) CFRelease(access);
    if (apps) CFRelease(apps);
    if (reader) CFRelease(reader);
    if (self) CFRelease(self);
    return status;
}
