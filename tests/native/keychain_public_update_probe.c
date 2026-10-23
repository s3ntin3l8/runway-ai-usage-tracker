/* Synthetic-only public-API probe for #773. Never built into Runway. */
#include <CoreFoundation/CoreFoundation.h>
#include <Security/Security.h>
#include <sys/stat.h>
#include <unistd.h>
#include <stdio.h>
#include <string.h>
#include <limits.h>
#include <stdlib.h>

static int private_fixture(const char *path) {
    char resolved[PATH_MAX], parent[PATH_MAX];
    struct stat file, directory;
    if (!realpath(path, resolved) || strcmp(path, resolved) ||
        lstat(path, &file) || !S_ISREG(file.st_mode) ||
        file.st_uid != getuid() || file.st_nlink != 1) return 0;
    snprintf(parent, sizeof(parent), "%s", path);
    char *slash = strrchr(parent, '/');
    if (!slash || strcmp(slash + 1, "fixture-0.keychain-db")) return 0;
    *slash = 0;
    char *name = strrchr(parent, '/');
    if (!name || strncmp(name + 1, "runway-native-526-", 18) ||
        stat(parent, &directory) || directory.st_uid != getuid() ||
        (directory.st_mode & 0777) != 0700) return 0;
    return 1;
}

static void reference_query(CFMutableDictionaryRef query, SecKeychainItemRef item) {
    CFDictionaryRemoveValue(query, kSecAttrService);
    CFDictionaryRemoveValue(query, kSecAttrAccount);
    CFDictionaryRemoveValue(query, kSecMatchLimit);
    CFDictionaryRemoveValue(query, kSecReturnRef);
    CFDictionarySetValue(query, kSecValueRef, item);
}

int main(int argc, char **argv) {
    fputs("stage:start\n", stderr);
    if (argc != 3 || !private_fixture(argv[1])) return 64;
    int combined = !strcmp(argv[2], "combined-access");
    int legacy = !strcmp(argv[2], "legacy-data");
    int metadata = !strcmp(argv[2], "metadata-control");
    int control = !strcmp(argv[2], "read-only-control");
    int reference_control = !strcmp(argv[2], "reference-control");
    if (!reference_control && !control && !combined && !legacy && !metadata && strcmp(argv[2], "data-only")) return 64;
    if (SecKeychainSetUserInteractionAllowed(false)) return 65;
    fputs("stage:interaction_disabled\n", stderr);
    SecKeychainRef keychain = NULL, owner = NULL, resolved_owner = NULL;
    SecAccessRef saved = NULL;
    CFTypeRef found = NULL, readback = NULL, resolution = NULL;
    CFArrayRef scope = NULL;
    CFMutableDictionaryRef query = NULL, update = NULL;
    CFDataRef replacement = NULL;
    int result = 66;
    OSStatus status = SecKeychainOpen(argv[1], &keychain), read_status = -1;
    if (status || !keychain) goto cleanup;
    fputs("stage:private_keychain_opened\n", stderr);
    scope = CFArrayCreate(NULL, (const void **)&keychain, 1, &kCFTypeArrayCallBacks);
    query = CFDictionaryCreateMutable(NULL, 0, &kCFTypeDictionaryKeyCallBacks,
                                     &kCFTypeDictionaryValueCallBacks);
    CFDictionarySetValue(query, kSecClass, kSecClassGenericPassword);
    CFDictionarySetValue(query, kSecAttrService, CFSTR("Runway synthetic public-update probe"));
    CFDictionarySetValue(query, kSecAttrAccount, CFSTR("runway-synthetic-acceptance"));
    CFDictionarySetValue(query, kSecMatchSearchList, scope);
    CFDictionarySetValue(query, kSecUseKeychain, keychain);
    CFDictionarySetValue(query, kSecUseDataProtectionKeychain, kCFBooleanFalse);
    CFDictionarySetValue(query, kSecMatchLimit, kSecMatchLimitAll);
    CFDictionarySetValue(query, kSecReturnRef, kCFBooleanTrue);
    fputs("stage:before_item_metadata\n", stderr);
    status = SecItemCopyMatching(query, &found);
    if (status || !found || CFGetTypeID(found) != CFArrayGetTypeID() ||
        CFArrayGetCount(found) != 1) goto cleanup;
    SecKeychainItemRef item = (SecKeychainItemRef)CFArrayGetValueAtIndex(found, 0);
    status = SecKeychainItemCopyKeychain(item, &owner);
    if (status || !owner || !CFEqual(owner, keychain)) goto cleanup;
    fputs("stage:owner_verified\n", stderr);
    if (control) {
        fputs("stage:control_complete\n", stderr);
        puts("{\"control_completed\":true}");
        result = 0;
        goto cleanup;
    }
    if (reference_control) {
        reference_query(query, item);
        fputs("stage:before_reference_lookup\n", stderr);
        status = SecItemCopyMatching(query, &resolution);
        if (status || !resolution) goto cleanup;
        CFTypeRef resolved = resolution;
        if (CFGetTypeID(resolved) == CFArrayGetTypeID()) {
            if (CFArrayGetCount((CFArrayRef)resolved) != 1) goto cleanup;
            resolved = CFArrayGetValueAtIndex((CFArrayRef)resolved, 0);
        }
        if (CFGetTypeID(resolved) != SecKeychainItemGetTypeID() || !CFEqual(resolved, item)) goto cleanup;
        status = SecKeychainItemCopyKeychain((SecKeychainItemRef)resolved, &resolved_owner);
        if (status || !resolved_owner || !CFEqual(resolved_owner, keychain)) goto cleanup;
        fputs("stage:reference_verified\nstage:control_complete\n", stderr);
        puts("{\"control_completed\":true}");
        result = 0;
        goto cleanup;
    }
    if (combined && SecKeychainItemCopyAccess(item, &saved)) goto cleanup;
    const unsigned char fake[] = "synthetic-public-update-replacement";
    replacement = CFDataCreate(NULL, fake, sizeof(fake) - 1);
    update = CFDictionaryCreateMutable(NULL, 0, &kCFTypeDictionaryKeyCallBacks,
                                      &kCFTypeDictionaryValueCallBacks);
    reference_query(query, item);
    if (metadata) CFDictionarySetValue(update, kSecAttrComment, CFSTR("synthetic-control"));
    else CFDictionarySetValue(update, kSecValueData, replacement);
    if (combined) CFDictionarySetValue(update, kSecAttrAccess, saved);
    fputs("stage:before_update\n", stderr);
    if (legacy)
        status = SecKeychainItemModifyAttributesAndData(item, NULL, sizeof(fake) - 1, fake);
    else
        status = SecItemUpdate(query, update);
    fputs("stage:after_update\n", stderr);
    CFDictionarySetValue(query, kSecReturnData, kCFBooleanTrue);
    read_status = SecItemCopyMatching(query, &readback);
    int verified = !read_status && readback && CFEqual(readback, replacement);
    printf("{\"update_status\":%d,\"read_status\":%d,\"replacement_verified\":%s}\n",
           (int)status, (int)read_status, verified ? "true" : "false");
    result = 0;
cleanup:
    if (resolved_owner) CFRelease(resolved_owner);
    if (resolution) CFRelease(resolution);
    if (readback) CFRelease(readback);
    if (replacement) CFRelease(replacement);
    if (update) CFRelease(update);
    if (saved) CFRelease(saved);
    if (found) CFRelease(found);
    if (query) CFRelease(query);
    if (scope) CFRelease(scope);
    if (owner) CFRelease(owner);
    if (keychain) CFRelease(keychain);
    return result;
}
