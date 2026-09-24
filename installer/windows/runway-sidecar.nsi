; Runway Sidecar — Windows installer (NSIS 3.x, per-user, no elevation).
;
; Build (CI: .github/workflows/sidecar-build.yml; local: `make sidecar-installer`):
;   makensis -DVERSION=<package.json version> -DPRODUCT_VERSION_QUAD=<X.Y.Z.0> \
;            -DOUTFILE=<dest.exe> installer/windows/runway-sidecar.nsi
; with the PyInstaller one-file build at dist/RunwaySidecar.exe. Both version
; values come from `python sidecar_app/spec/win_version.py`.
;
; Adapted from s3ntin3l8/branchdam-agent's installer. Design notes:
; * Per-user install into %LOCALAPPDATA%\Programs — no UAC prompt, and the
;   directory stays user-writable so the sidecar's in-place self-update
;   (scripts/sidecar_pkg/self_update.py:_apply_windows) keeps working.
; * Config / queue / logs live in %APPDATA%\runway\sidecar and are never
;   touched by install, upgrade or uninstall.
; * The login item is the SAME HKCU Run value the tray's "Launch at Login"
;   toggle manages (sidecar_app/autostart.py:_WIN_REG_KEY), so the finish-page
;   checkbox and the tray menu always agree. A contract test pins the name.
; * Silent install: `setup.exe /S [/AUTOSTART=1] [/D=C:\path]`.

Unicode True
RequestExecutionLevel user
SetCompressor /SOLID lzma

!include "MUI2.nsh"
!include "FileFunc.nsh"
!include "LogicLib.nsh"

; --- Version (populated by CI; zeroed defaults keep a bare local compile working) ---
!ifndef VERSION
    !define VERSION "0.0.0"
!endif
!ifndef PRODUCT_VERSION_QUAD
    !define PRODUCT_VERSION_QUAD "0.0.0.0"
!endif
!ifndef OUTFILE
    !define OUTFILE "..\..\dist\runway-sidecar-setup.exe"
!endif

!define PRODUCT_NAME "Runway Sidecar"
!define PRODUCT_PUBLISHER "Runway"
!define PRODUCT_WEB_SITE "https://github.com/s3ntin3l8/runway-ai-usage-tracker"
!define EXE_NAME "RunwaySidecar.exe"
!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}"
; Must equal sidecar_app/autostart.py:_WIN_REG_PATH / _WIN_REG_KEY.
!define RUN_KEY "Software\Microsoft\Windows\CurrentVersion\Run"
!define RUN_VALUE "Runway Sidecar"
; runway-sidecar://pair?… deep links from the dashboard (sidecar_app/url_events.py).
; Must match CFBundleURLSchemes in sidecar_app/spec/macos.spec (contract-tested).
!define URL_SCHEME "runway-sidecar"
!define URL_KEY "Software\Classes\${URL_SCHEME}"

Name "${PRODUCT_NAME}"
OutFile "${OUTFILE}"
InstallDir "$LOCALAPPDATA\Programs\${PRODUCT_NAME}"
InstallDirRegKey HKCU "${UNINSTALL_KEY}" "InstallLocation"
BrandingText "${PRODUCT_NAME} ${VERSION}"

; --- Version resource (Explorer > Properties > Details) ---
VIProductVersion "${PRODUCT_VERSION_QUAD}"
VIFileVersion "${PRODUCT_VERSION_QUAD}"
VIAddVersionKey "ProductName" "${PRODUCT_NAME}"
VIAddVersionKey "CompanyName" "${PRODUCT_PUBLISHER}"
VIAddVersionKey "LegalCopyright" "Runway contributors. Licensed under AGPL-3.0."
VIAddVersionKey "FileDescription" "${PRODUCT_NAME} Installer"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "ProductVersion" "${VERSION}"

; --- Interface (art is derived from assets/logo.svg by `make logo`) ---
!define MUI_ABORTWARNING
!define MUI_ICON "..\assets\app.ico"
!define MUI_UNICON "..\assets\app.ico"
!define MUI_WELCOMEFINISHPAGE_BITMAP "..\assets\installer-sidebar.bmp"
!define MUI_UNWELCOMEFINISHPAGE_BITMAP "..\assets\installer-sidebar.bmp"
!define MUI_HEADERIMAGE
!define MUI_HEADERIMAGE_RIGHT
!define MUI_HEADERIMAGE_BITMAP "..\assets\installer-header.bmp"
!define MUI_HEADERIMAGE_UNBITMAP "..\assets\installer-header.bmp"

!define MUI_WELCOMEPAGE_TEXT "Setup will install ${PRODUCT_NAME} ${VERSION}.$\r$\n$\r$\nThe sidecar runs in the notification area and forwards AI usage from this machine (local CLI logs, browser sessions) to your Runway server.$\r$\n$\r$\nClick Next to continue."

!define MUI_FINISHPAGE_RUN "$INSTDIR\${EXE_NAME}"
!define MUI_FINISHPAGE_RUN_TEXT "Start ${PRODUCT_NAME} now"
; The "show readme" checkbox is repurposed as the login-item toggle.
!define MUI_FINISHPAGE_SHOWREADME ""
!define MUI_FINISHPAGE_SHOWREADME_TEXT "Start ${PRODUCT_NAME} automatically when I sign in"
!define MUI_FINISHPAGE_SHOWREADME_FUNCTION EnableAutostart
!define MUI_FINISHPAGE_LINK "Runway documentation"
!define MUI_FINISHPAGE_LINK_LOCATION "${PRODUCT_WEB_SITE}/blob/main/docs/sidecar.md"

; --- Pages ---
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "..\..\LICENSE"
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

; --- Init ---
Function .onInit
    ; Per-user shell folders ($SMPROGRAMS, $DESKTOP, $APPDATA) — the whole
    ; no-elevation design depends on these resolving to the current user.
    SetShellVarContext current
FunctionEnd

Function un.onInit
    ; The uninstaller is a separate compiled context; set it again.
    SetShellVarContext current
FunctionEnd

; Abort with an actionable Retry/Cancel prompt (instead of NSIS's generic
; "can't write" error) when the sidecar is running. Probes via
; kernel32::CreateFile with exclusive write access, which Windows refuses
; with ERROR_SHARING_VIOLATION (32) only while the image is mapped for
; execution. Defined via !macro so installer and uninstaller (separate
; compiled contexts) share one implementation.
!macro CheckExeNotRunning un
Function ${un}CheckExeNotRunning
    Push $1
    Push $2
    retry_open:
    IfFileExists "$INSTDIR\${EXE_NAME}" 0 done
    System::Call 'kernel32::CreateFile(t "$INSTDIR\${EXE_NAME}", i 0x40000000, i 0, i 0, i 3, i 0, i 0) i .r1'
    IntCmp $1 -1 check_error opened opened
    check_error:
        System::Call 'kernel32::GetLastError() i .r2'
        IntCmp $2 32 in_use done done
    in_use:
        MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "${PRODUCT_NAME} is currently running.$\r$\n$\r$\nQuit it first (right-click the Runway icon in the notification area and choose Quit), then click Retry." /SD IDCANCEL IDRETRY retry_open
        Pop $2
        Pop $1
        Abort "${PRODUCT_NAME} is running."
    opened:
        System::Call 'kernel32::CloseHandle(i r1)'
    done:
    Pop $2
    Pop $1
FunctionEnd
!macroend
!insertmacro CheckExeNotRunning ""
!insertmacro CheckExeNotRunning "un."

Function EnableAutostart
    ; Quoted: the default install path contains a space.
    WriteRegStr HKCU "${RUN_KEY}" "${RUN_VALUE}" '"$INSTDIR\${EXE_NAME}"'
FunctionEnd

; --- Sections ---
Section "!${PRODUCT_NAME}" SecCore
    SectionIn RO
    Call CheckExeNotRunning

    SetOutPath "$INSTDIR"
    File "..\..\dist\${EXE_NAME}"
    ; Leftovers from an interrupted self-update would otherwise be swapped in
    ; later by a stale helper; start clean.
    Delete "$INSTDIR\RunwaySidecar.new.exe"
    Delete "$INSTDIR\runway-self-update.bat"

    WriteUninstaller "$INSTDIR\uninstall.exe"

    CreateDirectory "$SMPROGRAMS\${PRODUCT_NAME}"
    CreateShortCut "$SMPROGRAMS\${PRODUCT_NAME}\${PRODUCT_NAME}.lnk" "$INSTDIR\${EXE_NAME}"
    CreateShortCut "$SMPROGRAMS\${PRODUCT_NAME}\Uninstall ${PRODUCT_NAME}.lnk" "$INSTDIR\uninstall.exe"

    ; Apps & Features entry
    WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayName" "${PRODUCT_NAME}"
    WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayIcon" "$INSTDIR\${EXE_NAME}"
    WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayVersion" "${VERSION}"
    WriteRegStr HKCU "${UNINSTALL_KEY}" "Publisher" "${PRODUCT_PUBLISHER}"
    WriteRegStr HKCU "${UNINSTALL_KEY}" "URLInfoAbout" "${PRODUCT_WEB_SITE}"
    WriteRegStr HKCU "${UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
    WriteRegStr HKCU "${UNINSTALL_KEY}" "UninstallString" '"$INSTDIR\uninstall.exe"'
    WriteRegStr HKCU "${UNINSTALL_KEY}" "QuietUninstallString" '"$INSTDIR\uninstall.exe" /S'
    WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoModify" 1
    WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoRepair" 1
    ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
    IntFmt $0 "0x%08X" $0
    WriteRegDWORD HKCU "${UNINSTALL_KEY}" "EstimatedSize" "$0"

    ; Deep-link protocol handler (per-user; no elevation needed).
    WriteRegStr HKCU "${URL_KEY}" "" "URL:Runway Sidecar pairing link"
    WriteRegStr HKCU "${URL_KEY}" "URL Protocol" ""
    WriteRegStr HKCU "${URL_KEY}\DefaultIcon" "" '"$INSTDIR\${EXE_NAME}",0'
    WriteRegStr HKCU "${URL_KEY}\shell\open\command" "" '"$INSTDIR\${EXE_NAME}" "%1"'

    ; Silent installs have no finish page: honour /AUTOSTART=1 instead.
    ${If} ${Silent}
        ${GetParameters} $R0
        ClearErrors
        ${GetOptions} $R0 "/AUTOSTART=" $R1
        ${IfNot} ${Errors}
        ${AndIf} $R1 == "1"
            Call EnableAutostart
        ${EndIf}
    ${EndIf}
SectionEnd

Section /o "Desktop shortcut" SecDesktop
    CreateShortCut "$DESKTOP\${PRODUCT_NAME}.lnk" "$INSTDIR\${EXE_NAME}"
SectionEnd

!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
    !insertmacro MUI_DESCRIPTION_TEXT ${SecCore} "The sidecar tray app (required)."
    !insertmacro MUI_DESCRIPTION_TEXT ${SecDesktop} "Add a ${PRODUCT_NAME} shortcut to the desktop."
!insertmacro MUI_FUNCTION_DESCRIPTION_END

; --- Uninstaller ---
Section "Uninstall"
    Call un.CheckExeNotRunning

    DeleteRegValue HKCU "${RUN_KEY}" "${RUN_VALUE}"
    DeleteRegKey HKCU "${URL_KEY}"

    Delete "$INSTDIR\${EXE_NAME}"
    Delete "$INSTDIR\uninstall.exe"
    ; Self-update leftovers (scripts/sidecar_pkg/self_update.py): the staged
    ; exe + helper script, and the single-slot rollback backup.
    Delete "$INSTDIR\RunwaySidecar.new.exe"
    Delete "$INSTDIR\runway-self-update.bat"
    Delete "$INSTDIR\${EXE_NAME}.previous"
    Delete "$INSTDIR\${EXE_NAME}.previous.version"
    Delete "$INSTDIR\${EXE_NAME}.old"
    RMDir "$INSTDIR"

    Delete "$SMPROGRAMS\${PRODUCT_NAME}\${PRODUCT_NAME}.lnk"
    Delete "$SMPROGRAMS\${PRODUCT_NAME}\Uninstall ${PRODUCT_NAME}.lnk"
    RMDir "$SMPROGRAMS\${PRODUCT_NAME}"
    Delete "$DESKTOP\${PRODUCT_NAME}.lnk"

    DeleteRegKey HKCU "${UNINSTALL_KEY}"
    ; %APPDATA%\runway\sidecar (config, queue, logs) is deliberately kept so
    ; an uninstall/reinstall cycle never loses the server URL and API key.
SectionEnd
