#include "support.h"

volatile LONG interrupted = 0;

BOOL WINAPI control(DWORD type) {
    if (type == CTRL_C_EVENT || type == CTRL_BREAK_EVENT) {
        InterlockedExchange(&interrupted, 1);
        return TRUE;
    }
    return FALSE;
}

int join_path(wchar_t *out, const wchar_t *dir, const wchar_t *file) {
    int n = swprintf(out, PATH_CAP, L"%ls\\%ls", dir, file);
    if (n >= 0 && n < PATH_CAP) return 1;
    SetLastError(ERROR_FILENAME_EXCED_RANGE);
    return 0;
}

int write_all(HANDLE file, const char *bytes, DWORD length) {
    while (length) {
        DWORD written = 0;
        if (!WriteFile(file, bytes, length, &written, NULL)) return 0;
        if (!written) { SetLastError(ERROR_WRITE_FAULT); return 0; }
        bytes += written;
        length -= written;
    }
    return FlushFileBuffers(file) != 0;
}

/* No command, environment or user path enters the machine-readable journal. */
int event(Journal *j, const char *name, ULONGLONG commit,
                 DWORD active, DWORD code) {
    char line[512];
    int n = snprintf(line, sizeof(line),
        "{\"version\":1,\"run_id\":\"%s\",\"event\":\"%s\","
        "\"elapsed_ms\":%llu,\"job_commit_bytes\":%llu,"
        "\"active_processes\":%lu,\"code\":%lu}\n",
        j->nonce, name, GetTickCount64() - j->started, commit,
        (unsigned long)active, (unsigned long)code);
    if (n <= 0 || (size_t)n >= sizeof(line)) {
        SetLastError(ERROR_INSUFFICIENT_BUFFER);
        return 0;
    }
    return write_all(j->log, line, (DWORD)n);
}

int number(const wchar_t *text, ULONGLONG maximum, ULONGLONG *value) {
    if (!text[0] || text[0] < L'0' || text[0] > L'9') return 0;
    wchar_t *end = NULL;
    errno = 0;
    unsigned long long parsed = wcstoull(text, &end, 10);
    if (errno || *end || parsed > maximum) return 0;
    *value = parsed;
    return 1;
}

static int append(wchar_t *out, size_t *used, wchar_t c) {
    if (*used >= CMD_CAP - 1) {
        SetLastError(ERROR_INSUFFICIENT_BUFFER);
        return 0;
    }
    out[(*used)++] = c;
    out[*used] = 0;
    return 1;
}

/* Quote argv for the Windows C runtime; never invoke a shell implicitly. */
int command_line(wchar_t *out, int count, wchar_t **args) {
    size_t used = 0;
    out[0] = 0;
    if (count <= 0) { SetLastError(ERROR_INVALID_PARAMETER); return 0; }
    for (int i = 0; i < count; ++i) {
        if (i && !append(out, &used, L' ')) return 0;
        if (!append(out, &used, L'"')) return 0;
        size_t slashes = 0;
        for (const wchar_t *p = args[i]; ; ++p) {
            if (*p == L'\\') { ++slashes; continue; }
            size_t emit = (*p == L'"' || *p == 0) ? slashes * 2 : slashes;
            if (*p == L'"') ++emit;
            while (emit--) if (!append(out, &used, L'\\')) return 0;
            slashes = 0;
            if (!*p) break;
            if (!append(out, &used, *p)) return 0;
        }
        if (!append(out, &used, L'"')) return 0;
    }
    return 1;
}

int request_stop(const wchar_t *dir, Journal *j) {
    wchar_t path[PATH_CAP], temporary[PATH_CAP];
    char body[128];
    if (!join_path(path, dir, L"stop.request")
        || !join_path(temporary, dir, L"stop.request.tmp")) return 0;
    int n = snprintf(body, sizeof(body), "headroom-stop-v1\n%s\n", j->nonce);
    HANDLE file = CreateFileW(temporary, GENERIC_WRITE, FILE_SHARE_READ, NULL,
        CREATE_NEW, FILE_ATTRIBUTE_NORMAL | FILE_FLAG_WRITE_THROUGH, NULL);
    if (file == INVALID_HANDLE_VALUE) return 0;
    int ok = write_all(file, body, (DWORD)n);
    DWORD error = ok ? ERROR_SUCCESS : GetLastError();
    if (!CloseHandle(file) && ok) { ok = 0; error = GetLastError(); }
    /* Publish only complete bytes; a cooperative task must not read a prefix. */
    if (ok && MoveFileExW(temporary, path, MOVEFILE_WRITE_THROUGH)) return 1;
    if (ok) error = GetLastError();
    DeleteFileW(temporary); /* Only the temporary file we successfully created. */
    SetLastError(error);
    return 0;
}

/* Even CreateFile can wait indefinitely for another process's oplock. Keep
   the entire open/inspect/read operation off the policy thread. There is at
   most one outstanding check, its storage outlives its thread, and no journal,
   job or caller-owned handles/pointers are retained. A stuck filesystem cannot
   create an unbounded number of workers or delay termination of an owned job. */
typedef struct {
    HANDLE thread;
    wchar_t path[PATH_CAP];
    char expected[128];
    DWORD length;
    int accepted;
} AckCheck;

static SRWLOCK ack_lock = SRWLOCK_INIT;
static AckCheck ack_check;

static DWORD WINAPI read_ack(void *context) {
    AckCheck *check = context;
    char actual[129];
    HANDLE file = CreateFileW(check->path, GENERIC_READ, FILE_SHARE_READ, NULL,
        OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT, NULL);
    if (file == INVALID_HANDLE_VALUE) return 0;
    BY_HANDLE_FILE_INFORMATION info;
    DWORD got = 0;
    check->accepted = GetFileInformationByHandle(file, &info)
        && !(info.dwFileAttributes & (FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_DIRECTORY))
        && !info.nFileSizeHigh && info.nFileSizeLow == check->length
        && GetFileType(file) == FILE_TYPE_DISK
        && ReadFile(file, actual, sizeof(actual), &got, NULL)
        && got == check->length && !memcmp(actual, check->expected, got);
    CloseHandle(file);
    return 0;
}

int acknowledged(const wchar_t *dir, const Journal *j) {
    wchar_t path[PATH_CAP];
    char expected[128];
    if (!join_path(path, dir, L"stop.ack")) return 0;
    int n = snprintf(expected, sizeof(expected), "headroom-stop-v1\n%s\n", j->nonce);
    if (n <= 0 || (size_t)n >= sizeof(expected)) return 0;
    if (!TryAcquireSRWLockExclusive(&ack_lock)) return 0;
    int accepted = 0;
    if (ack_check.thread) {
        if (WaitForSingleObject(ack_check.thread, 0) != WAIT_OBJECT_0) {
            CancelSynchronousIo(ack_check.thread);
            goto done;
        }
        CloseHandle(ack_check.thread);
        ack_check.thread = NULL;
        accepted = ack_check.accepted && !wcscmp(path, ack_check.path)
            && ack_check.length == (DWORD)n && !memcmp(expected, ack_check.expected, (size_t)n);
        if (accepted) goto done;
    }
    wcscpy(ack_check.path, path);
    memcpy(ack_check.expected, expected, (size_t)n);
    ack_check.length = (DWORD)n;
    ack_check.accepted = 0;
    ack_check.thread = CreateThread(NULL, 0, read_ack, &ack_check, 0, NULL);
    /* A short bounded wait also catches a task which writes its ack and exits
       before the next policy cycle. Slow checks may conservatively miss an ack. */
    if (ack_check.thread && WaitForSingleObject(ack_check.thread, 5) == WAIT_OBJECT_0) {
        accepted = ack_check.accepted;
        CloseHandle(ack_check.thread);
        ack_check.thread = NULL;
    }
done:
    ReleaseSRWLockExclusive(&ack_lock);
    return accepted;
}
