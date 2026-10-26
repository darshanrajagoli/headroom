#include "output.h"

static void close_handle(HANDLE *handle) {
    if (*handle && *handle != INVALID_HANDLE_VALUE) CloseHandle(*handle);
    *handle = NULL;
}

void hr_output_child_done(HrOutput *out) {
    DWORD error = GetLastError();
    for (int i = 0; i < 3; ++i) close_handle(out->children + i);
    if (out->attributes_initialized) DeleteProcThreadAttributeList(out->attributes);
    free(out->attributes);
    out->attributes = NULL;
    out->attributes_initialized = 0;
    SetLastError(error);
}

void hr_output_close(HrOutput *out) {
    DWORD error = GetLastError();
    hr_output_child_done(out);
    for (int i = 0; i < 2; ++i) {
        close_handle(&out->streams[i].reader);
        close_handle(&out->streams[i].file);
    }
    SetLastError(error);
}

int hr_output_prepare(HrOutput *out, const wchar_t *directory, STARTUPINFOEXW *startup) {
    SECURITY_ATTRIBUTES inherit = {sizeof(inherit), NULL, TRUE};
    const wchar_t *names[2] = {L"stdout.log", L"stderr.log"};
    memset(out, 0, sizeof(*out));
    memset(startup, 0, sizeof(*startup));
    startup->StartupInfo.cb = sizeof(*startup);
    for (int i = 0; i < 2; ++i) {
        HrOutputStream *s = out->streams + i;
        wchar_t path[PATH_CAP];
        if (!join_path(path, directory, names[i])) return 0;
        s->file = CreateFileW(path, GENERIC_WRITE, FILE_SHARE_READ, NULL,
            CREATE_NEW, FILE_ATTRIBUTE_NORMAL, NULL);
        if (s->file == INVALID_HANDLE_VALUE) return 0;
        if (!CreatePipe(&s->reader, out->children + i + 1, &inherit, 4096)
            || !SetHandleInformation(s->reader, HANDLE_FLAG_INHERIT, 0)) return 0;
    }
    out->children[0] = CreateFileW(L"NUL", GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
        &inherit, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (out->children[0] == INVALID_HANDLE_VALUE) return 0;
    SIZE_T size = 0;
    /* A second slot binds Job Objects atomically in CreateProcessW. */
    InitializeProcThreadAttributeList(NULL, 2, 0, &size);
    if (!size) return 0;
    out->attributes = calloc(1, size);
    if (!out->attributes) { SetLastError(ERROR_NOT_ENOUGH_MEMORY); return 0; }
    if (!InitializeProcThreadAttributeList(out->attributes, 2, 0, &size)) return 0;
    out->attributes_initialized = 1;
    if (!UpdateProcThreadAttribute(out->attributes, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
        out->children, sizeof(out->children), NULL, NULL)) return 0;
    startup->lpAttributeList = out->attributes;
    startup->StartupInfo.dwFlags = STARTF_USESTDHANDLES;
    startup->StartupInfo.hStdInput = out->children[0];
    startup->StartupInfo.hStdOutput = out->children[1];
    startup->StartupInfo.hStdError = out->children[2];
    out->ready = 1;
    return 1;
}

static int write_chunk(HANDLE file, const char *bytes, DWORD length) {
    while (length) {
        DWORD written = 0;
        if (!WriteFile(file, bytes, length, &written, NULL)) return 0;
        if (!written) { SetLastError(ERROR_WRITE_FAULT); return 0; }
        bytes += written;
        length -= written;
    }
    return 1;
}

int hr_output_drain(HrOutput *out, int *activity) {
    if (!out->ready || out->finalized) return 1;
    char buffer[4096];
    for (int i = 0; i < 2; ++i) {
        HrOutputStream *s = out->streams + i;
        DWORD allowance = 65536;
        while (!s->eof && allowance) {
            DWORD available = 0;
            if (!PeekNamedPipe(s->reader, NULL, 0, NULL, &available, NULL)) {
                DWORD error = GetLastError();
                if (error != ERROR_BROKEN_PIPE) return 0;
                s->eof = 1;
                close_handle(&s->reader);
                break;
            }
            if (!available) break;
            DWORD requested = available < sizeof(buffer) ? available : (DWORD)sizeof(buffer);
            if (requested > allowance) requested = allowance;
            DWORD got = 0;
            /* This handle has exactly one reader. No other thread can consume
             * the bytes observed by PeekNamedPipe before this bounded read. */
            if (!ReadFile(s->reader, buffer, requested, &got, NULL)) {
                if (GetLastError() != ERROR_BROKEN_PIPE) return 0;
                s->eof = 1;
                close_handle(&s->reader);
                break;
            }
            if (!got) break;
            allowance -= got;
            *activity = 1;
            uint64_t room = HR_OUTPUT_LIMIT - s->retained;
            DWORD keep = room < got ? (DWORD)room : got;
            if (keep && !write_chunk(s->file, buffer, keep)) return 0;
            s->retained += keep;
            if (UINT64_MAX - s->discarded < got - keep) {
                SetLastError(ERROR_ARITHMETIC_OVERFLOW); return 0;
            }
            s->discarded += got - keep;
        }
    }
    return 1;
}

int hr_output_finish(HrOutput *out, const wchar_t *directory) {
    if (out->finalized) return 1;
    if (!out->ready) { SetLastError(ERROR_INVALID_STATE); return 0; }
    int activity = 0;
    if (!hr_output_drain(out, &activity)) return 0;
    if (!out->streams[0].eof || !out->streams[1].eof) return 2;
    for (int i = 0; i < 2; ++i)
        if (!FlushFileBuffers(out->streams[i].file)) return 0;
    char body[512];
    int n = snprintf(body, sizeof(body),
        "{\"version\":1,\"limit_bytes\":%llu,"
        "\"stdout\":{\"retained_bytes\":%llu,\"discarded_bytes\":%llu,\"eof\":true},"
        "\"stderr\":{\"retained_bytes\":%llu,\"discarded_bytes\":%llu,\"eof\":true}}\n",
        (unsigned long long)HR_OUTPUT_LIMIT,
        (unsigned long long)out->streams[0].retained, (unsigned long long)out->streams[0].discarded,
        (unsigned long long)out->streams[1].retained, (unsigned long long)out->streams[1].discarded);
    if (n < 0 || (size_t)n >= sizeof(body)) { SetLastError(ERROR_INSUFFICIENT_BUFFER); return 0; }
    wchar_t path[PATH_CAP];
    if (!join_path(path, directory, L"output.json")) return 0;
    HANDLE file = CreateFileW(path, GENERIC_WRITE, FILE_SHARE_READ, NULL, CREATE_NEW,
        FILE_ATTRIBUTE_NORMAL | FILE_FLAG_WRITE_THROUGH, NULL);
    if (file == INVALID_HANDLE_VALUE) return 0;
    int ok = write_all(file, body, (DWORD)n);
    DWORD error = GetLastError();
    CloseHandle(file);
    if (!ok) { SetLastError(error); return 0; }
    out->finalized = 1;
    return 1;
}
