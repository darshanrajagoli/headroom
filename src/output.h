#ifndef HEADROOM_OUTPUT_H
#define HEADROOM_OUTPUT_H
#include "support.h"

#define HR_OUTPUT_LIMIT 1048576ULL
typedef struct {
    HANDLE reader, file;
    uint64_t retained, discarded;
    int eof;
} HrOutputStream;
typedef struct {
    HrOutputStream streams[2];
    HANDLE children[3];
    LPPROC_THREAD_ATTRIBUTE_LIST attributes;
    int attributes_initialized, ready, finalized;
} HrOutput;

/* Own all handles even on failure. Close with hr_output_close. */
int hr_output_prepare(HrOutput *out, const wchar_t *directory, STARTUPINFOEXW *startup);
/* Immediately after CreateProcess: no coordinator copy of child pipe writers. */
void hr_output_child_done(HrOutput *out);
/* One owner thread; bounded reads into one 4 KiB stack buffer. */
int hr_output_drain(HrOutput *out, int *activity);
/* Only after the owned group is empty: 1 flushed, 2 still draining, 0 failure. */
int hr_output_finish(HrOutput *out, const wchar_t *directory);
void hr_output_close(HrOutput *out);
#endif
