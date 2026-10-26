/* Fixture-only forced include, then a separately compiled interception unit.
 * No production source or production executable is modified. */
#if defined(HEADROOM_LAUNCH_PROBE_CHILD)
#include "../src/support.h"
int wmain(void) { return 0; }
#elif defined(HEADROOM_LAUNCH_PROBE_HEADER)
#define _WIN32_WINNT 0x0A00
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
BOOL WINAPI hr_probe_CreateProcessW(LPCWSTR, LPWSTR, LPSECURITY_ATTRIBUTES,
    LPSECURITY_ATTRIBUTES, BOOL, DWORD, LPVOID, LPCWSTR, LPSTARTUPINFOW,
    LPPROCESS_INFORMATION);
BOOL WINAPI hr_probe_QueryInformationJobObject(HANDLE, JOBOBJECTINFOCLASS,
    LPVOID, DWORD, LPDWORD);
ULONGLONG WINAPI hr_probe_GetTickCount64(void);
#define CreateProcessW hr_probe_CreateProcessW
#define QueryInformationJobObject hr_probe_QueryInformationJobObject
#define GetTickCount64 hr_probe_GetTickCount64
#else
#include "../src/support.h"

static volatile LONG created, triggered;
static HANDLE child_process;
static ULONGLONG tick_offset;

static int mode_is(const wchar_t *expected) {
    wchar_t mode[32];
    DWORD n = GetEnvironmentVariableW(L"HEADROOM_LAUNCH_PROBE_MODE", mode, 32);
    return n && n < 32 && !wcscmp(mode, expected);
}

static HANDLE open_event(const wchar_t *variable, DWORD access) {
    wchar_t name[128];
    DWORD n = GetEnvironmentVariableW(variable, name, 128);
    return n && n < 128 ? OpenEventW(access, FALSE, name) : NULL;
}

static void publish(const wchar_t *name, const char *body) {
    wchar_t directory[PATH_CAP], path[PATH_CAP], temporary[PATH_CAP];
    DWORD n = GetEnvironmentVariableW(L"HEADROOM_LAUNCH_PROBE_DIR", directory, PATH_CAP);
    if (!n || n >= PATH_CAP || !join_path(path, directory, name)) ExitProcess(91);
    size_t units = wcslen(path);
    if (units + 4 >= PATH_CAP) ExitProcess(91);
    wcscpy(temporary, path); wcscat(temporary, L".tmp");
    HANDLE file = CreateFileW(temporary, GENERIC_WRITE, FILE_SHARE_READ, NULL,
        CREATE_NEW, FILE_ATTRIBUTE_NORMAL | FILE_FLAG_WRITE_THROUGH, NULL);
    if (file == INVALID_HANDLE_VALUE) ExitProcess(91);
    int ok = write_all(file, body, (DWORD)strlen(body));
    if (!CloseHandle(file) || !ok
        || !MoveFileExW(temporary, path, MOVEFILE_WRITE_THROUGH)) ExitProcess(91);
}

BOOL WINAPI hr_probe_CreateProcessW(LPCWSTR application, LPWSTR command,
    LPSECURITY_ATTRIBUTES process_attributes, LPSECURITY_ATTRIBUTES thread_attributes,
    BOOL inherit, DWORD flags, LPVOID environment, LPCWSTR directory,
    LPSTARTUPINFOW startup, LPPROCESS_INFORMATION process) {
    BOOL ok = CreateProcessW(application, command, process_attributes, thread_attributes,
        inherit, flags, environment, directory, startup, process);
    DWORD error = GetLastError();
    if (ok) {
        char body[160];
        child_process = process->hProcess; /* Borrowed only until the first empty query. */
        InterlockedExchange(&created, 1);
        snprintf(body, sizeof(body), "{\"child_pid\":%lu,\"creation_flags\":%lu}\n",
            (unsigned long)process->dwProcessId, (unsigned long)flags);
        publish(L"created.json", body);
        HANDLE ready = open_event(L"HEADROOM_LAUNCH_PROBE_READY", EVENT_MODIFY_STATE);
        if (!ready || !SetEvent(ready)) ExitProcess(92);
        CloseHandle(ready);
        /* Every case waits until Python pins the suspended child identity.
         * The crash case deliberately never releases this barrier. */
        HANDLE release = open_event(L"HEADROOM_LAUNCH_PROBE_RELEASE", SYNCHRONIZE);
        if (!release || WaitForSingleObject(release, 20000) != WAIT_OBJECT_0)
            ExitProcess(93);
        CloseHandle(release);
    }
    SetLastError(error);
    return ok;
}

BOOL WINAPI hr_probe_QueryInformationJobObject(HANDLE job, JOBOBJECTINFOCLASS kind,
    LPVOID information, DWORD size, LPDWORD returned) {
    BOOL ok = QueryInformationJobObject(job, kind, information, size, returned);
    DWORD error = GetLastError();
    if (ok && kind == JobObjectBasicAccountingInformation
        && size >= sizeof(JOBOBJECT_BASIC_ACCOUNTING_INFORMATION)
        && !((JOBOBJECT_BASIC_ACCOUNTING_INFORMATION *)information)->ActiveProcesses
        && InterlockedCompareExchange(&created, 0, 0)
        && WaitForSingleObject(child_process, 0) == WAIT_OBJECT_0
        && (mode_is(L"deadline") || mode_is(L"interrupt"))
        && !InterlockedCompareExchange(&triggered, 1, 0)) {
        if (mode_is(L"deadline")) tick_offset = 60000;
        else control(CTRL_BREAK_EVENT); /* The production handler, synchronously. */
        publish(L"injected.json", mode_is(L"deadline")
            ? "{\"condition\":\"deadline\",\"offset_ms\":60000,\"active_processes\":0}\n"
            : "{\"condition\":\"interrupt\",\"active_processes\":0}\n");
    }
    SetLastError(error);
    return ok;
}

ULONGLONG WINAPI hr_probe_GetTickCount64(void) {
    return GetTickCount64() + tick_offset;
}
#endif
