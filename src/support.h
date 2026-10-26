#ifndef HEADROOM_SUPPORT_H
#define HEADROOM_SUPPORT_H
#define _WIN32_WINNT 0x0A00
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <objbase.h>
#include <psapi.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <wchar.h>
#include <errno.h>
#include <limits.h>
#include <string.h>

#define PATH_CAP 4096
#define CMD_CAP 32767
#define MIB (1024ULL * 1024ULL)

typedef struct {
    HANDLE log;
    ULONGLONG started;
    char nonce[64];
} Journal;

extern volatile LONG interrupted;
BOOL WINAPI control(DWORD type);
int join_path(wchar_t *out, const wchar_t *dir, const wchar_t *file);
int write_all(HANDLE file, const char *bytes, DWORD length);
int event(Journal *j, const char *name, ULONGLONG commit, DWORD active, DWORD code);
int number(const wchar_t *text, ULONGLONG maximum, ULONGLONG *value);
int command_line(wchar_t *out, int count, wchar_t **args);
int request_stop(const wchar_t *dir, Journal *j);
/* At most 5 ms of waiting; slow/unreadable/nonmatching acks return false.
   An outstanding check owns its storage and never retains caller pointers. */
int acknowledged(const wchar_t *dir, const Journal *j);
#endif
