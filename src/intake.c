#include "intake.h"

struct HrIntake {
    wchar_t directory[PATH_CAP];
    char nonce[64];
    HANDLE scan, ready, stop, thread;
    HANDLE enumeration;
    WIN32_FIND_DATAW next;
    volatile LONG pending;
    HrIntakeResult result;
};

static int uuid_name(const wchar_t *name) {
    if (wcslen(name) != 42 || wcscmp(name + 36, L".ready")) return 0;
    for (int i = 0; i < 36; ++i) {
        wchar_t c = name[i];
        if (i == 8 || i == 13 || i == 18 || i == 23) {
            if (c != L'-') return 0;
        } else if (!((c >= L'0' && c <= L'9') || (c >= L'a' && c <= L'f'))) return 0;
    }
    return 1;
}

static HANDLE open_regular(const wchar_t *path, DWORD maximum, DWORD *length) {
    HANDLE file = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ, NULL,
        OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT, NULL);
    if (file == INVALID_HANDLE_VALUE) return file;
    BY_HANDLE_FILE_INFORMATION info;
    LARGE_INTEGER size;
    if (!GetFileInformationByHandle(file, &info) || !GetFileSizeEx(file, &size)
        || GetFileType(file) != FILE_TYPE_DISK || info.nNumberOfLinks != 1
        || info.dwFileAttributes & (FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_DIRECTORY)
        || size.QuadPart < 1 || size.QuadPart > maximum) {
        CloseHandle(file); SetLastError(ERROR_INVALID_DATA); return INVALID_HANDLE_VALUE;
    }
    *length = (DWORD)size.QuadPart;
    return file;
}

static int exact_file(const wchar_t *path, const char *expected, DWORD length) {
    DWORD size = 0, got = 0;
    HANDLE file = open_regular(path, 128, &size);
    if (file == INVALID_HANDLE_VALUE) return 0;
    char body[128];
    int ok = size == length && ReadFile(file, body, size, &got, NULL)
        && got == size && !memcmp(body, expected, size);
    DWORD error = GetLastError();
    CloseHandle(file);
    if (!ok) SetLastError(error ? error : ERROR_INVALID_DATA);
    return ok;
}

static int request_layout(const wchar_t *directory) {
    wchar_t pattern[PATH_CAP];
    if (!join_path(pattern, directory, L"*")) return 0;
    WIN32_FIND_DATAW entry;
    HANDLE search = FindFirstFileW(pattern, &entry);
    if (search == INVALID_HANDLE_VALUE) return 0;
    unsigned int count = 0;
    int ok = 1;
    do {
        if (!wcscmp(entry.cFileName, L".") || !wcscmp(entry.cFileName, L"..")) continue;
        if (entry.dwFileAttributes & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT)
            || (wcscmp(entry.cFileName, L"target") && wcscmp(entry.cFileName, L"plan.hrp")
                && wcscmp(entry.cFileName, L"plan.json")) || ++count > 3) { ok = 0; break; }
    } while (FindNextFileW(search, &entry));
    DWORD error = GetLastError();
    FindClose(search);
    return ok && count == 3 && error == ERROR_NO_MORE_FILES;
}

static void discover(HrIntake *in) {
    HrIntakeResult *r = &in->result;
    wchar_t path[PATH_CAP], inbox[PATH_CAP], processed[PATH_CAP];
    if (!join_path(path, in->directory, L"seal.request")
        || !join_path(inbox, in->directory, L"inbox")
        || !join_path(processed, in->directory, L"processed")) goto fatal;
    DWORD attributes = GetFileAttributesW(path);
    if (attributes != INVALID_FILE_ATTRIBUTES) {
        char expected[128];
        int n = snprintf(expected, sizeof(expected), "headroom-seal-v1\n%s\n", in->nonce);
        if (n <= 0 || n >= (int)sizeof(expected)
            || !exact_file(path, expected, (DWORD)n)) goto fatal;
        r->kind = 2; return;
    }
    if (GetLastError() != ERROR_FILE_NOT_FOUND) goto fatal;
    if (!join_path(path, inbox, L"*")) goto fatal;
    if (!in->enumeration) {
        in->enumeration = FindFirstFileW(path, &in->next);
        if (in->enumeration == INVALID_HANDLE_VALUE) {
            DWORD error = GetLastError();
            in->enumeration = NULL;
            if (error == ERROR_FILE_NOT_FOUND) return;
            SetLastError(error); goto fatal;
        }
    }
    unsigned int inspected = 0;
    while (in->enumeration) {
        if (++inspected > 256 || WaitForSingleObject(in->stop, 0) == WAIT_OBJECT_0) break;
        WIN32_FIND_DATAW item = in->next;
        /* Retain the directory cursor across bounded scans, and advance it
         * before returning a request. Unknown prefixes/duplicates cannot
         * permanently hide later valid submissions. */
        if (!FindNextFileW(in->enumeration, &in->next)) {
            DWORD error = GetLastError();
            FindClose(in->enumeration); in->enumeration = NULL;
            if (error != ERROR_NO_MORE_FILES) { SetLastError(error); goto fatal; }
        }
        if (!uuid_name(item.cFileName) || !(item.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)
            || item.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT) continue;
        wchar_t id[37], source[PATH_CAP];
        wmemcpy(id, item.cFileName, 36); id[36] = 0;
        if (!join_path(r->directory, processed, id) || !join_path(source, inbox, item.cFileName)) {
            goto fatal;
        }
        if (GetFileAttributesW(r->directory) != INVALID_FILE_ATTRIBUTES) continue;
        if (GetLastError() != ERROR_FILE_NOT_FOUND) goto fatal;
        if (!MoveFileExW(source, r->directory, MOVEFILE_WRITE_THROUGH)) {
            DWORD error = GetLastError();
            if (error == ERROR_FILE_NOT_FOUND || error == ERROR_PATH_NOT_FOUND
                || error == ERROR_ALREADY_EXISTS || error == ERROR_FILE_EXISTS
                || error == ERROR_ACCESS_DENIED || error == ERROR_SHARING_VIOLATION
                || error == ERROR_LOCK_VIOLATION) continue;
            goto fatal;
        }
        for (int i = 0; i < 36; ++i) r->request_id[i] = (char)id[i];
        r->kind = 1;
        attributes = GetFileAttributesW(r->directory);
        if (attributes == INVALID_FILE_ATTRIBUTES || !(attributes & FILE_ATTRIBUTE_DIRECTORY)
            || attributes & FILE_ATTRIBUTE_REPARSE_POINT || !request_layout(r->directory)) { r->code = 64; return; }
        char expected[128];
        int n = snprintf(expected, sizeof(expected), "headroom-submit-v1\n%s\n%s\n", in->nonce, r->request_id);
        if (n <= 0 || n >= (int)sizeof(expected)
            || !join_path(path, r->directory, L"target")
            || !exact_file(path, expected, (DWORD)n)) { r->code = 64; return; }
        DWORD length;
        if (!join_path(path, r->directory, L"plan.json")) { r->code = 64; return; }
        HANDLE file = open_regular(path, 1048576, &length);
        if (file == INVALID_HANDLE_VALUE) { r->code = 64; return; }
        CloseHandle(file);
        if (!join_path(path, r->directory, L"plan.hrp")) { r->code = 64; return; }
        file = open_regular(path, 524288, &length);
        if (file == INVALID_HANDLE_VALUE) { r->code = 64; return; }
        r->plan = malloc(length);
        if (!r->plan) { CloseHandle(file); SetLastError(ERROR_NOT_ENOUGH_MEMORY); goto fatal; }
        DWORD got = 0;
        int ok = ReadFile(file, r->plan, length, &got, NULL) && got == length;
        CloseHandle(file);
        if (!ok) { free(r->plan); r->plan = NULL; r->code = 64; return; }
        r->length = length;
        return;
    }
    return;
fatal:
    r->kind = 3;
    r->code = GetLastError();
    if (!r->code) r->code = ERROR_GEN_FAILURE;
}

static DWORD WINAPI intake_worker(void *context) {
    HrIntake *in = context;
    HANDLE waits[2] = {in->stop, in->scan};
    for (;;) {
        DWORD result = WaitForMultipleObjects(2, waits, FALSE, INFINITE);
        if (result == WAIT_OBJECT_0) return 0;
        if (result != WAIT_OBJECT_0 + 1) return 1;
        discover(in);
        if (!SetEvent(in->ready)) return 1;
    }
}

HrIntake *hr_intake_start(const wchar_t *directory, const char *nonce) {
    HrIntake *in = calloc(1, sizeof(*in));
    if (!in) { SetLastError(ERROR_NOT_ENOUGH_MEMORY); return NULL; }
    if (wcslen(directory) >= PATH_CAP || strlen(nonce) >= sizeof(in->nonce)) {
        free(in); SetLastError(ERROR_INSUFFICIENT_BUFFER); return NULL;
    }
    wcscpy(in->directory, directory); strcpy(in->nonce, nonce);
    in->scan = CreateEventW(NULL, FALSE, FALSE, NULL);
    in->ready = CreateEventW(NULL, TRUE, FALSE, NULL);
    in->stop = CreateEventW(NULL, TRUE, FALSE, NULL);
    if (!in->scan || !in->ready || !in->stop) { hr_intake_close(in); return NULL; }
    /* Fixed worker count; 256 KiB reserved stack, committed as used. */
    in->thread = CreateThread(NULL, 262144, intake_worker, in,
        STACK_SIZE_PARAM_IS_A_RESERVATION, NULL);
    if (!in->thread) { hr_intake_close(in); return NULL; }
    return in;
}

int hr_intake_schedule(HrIntake *in) {
    if (InterlockedCompareExchange(&in->pending, 1, 0)) return 1;
    if (!SetEvent(in->scan)) { InterlockedExchange(&in->pending, 0); return 0; }
    return 1;
}

int hr_intake_poll(HrIntake *in, HrIntakeResult *result) {
    DWORD ready = WaitForSingleObject(in->ready, 0);
    if (ready == WAIT_TIMEOUT) {
        if (WaitForSingleObject(in->thread, 0) == WAIT_OBJECT_0) { SetLastError(ERROR_GEN_FAILURE); return -1; }
        return 0;
    }
    if (ready != WAIT_OBJECT_0 || !ResetEvent(in->ready)) return -1;
    *result = in->result;
    memset(&in->result, 0, sizeof(in->result));
    InterlockedExchange(&in->pending, 0);
    return 1;
}

void hr_intake_close(HrIntake *in) {
    if (!in) return;
    DWORD error = GetLastError();
    if (in->stop) SetEvent(in->stop);
    if (in->thread) {
        CancelSynchronousIo(in->thread);
        if (WaitForSingleObject(in->thread, 10) != WAIT_OBJECT_0) {
            /* Worker owns this allocation and copied paths only. Do not free
             * live storage or terminate its thread; process exit reclaims it. */
            SetLastError(error); return;
        }
        CloseHandle(in->thread);
    }
    if (in->scan) CloseHandle(in->scan);
    if (in->ready) CloseHandle(in->ready);
    if (in->stop) CloseHandle(in->stop);
    if (in->enumeration) FindClose(in->enumeration);
    free(in->result.plan);
    free(in);
    SetLastError(error);
}
