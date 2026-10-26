#ifndef HEADROOM_INTAKE_H
#define HEADROOM_INTAKE_H
#include "support.h"

typedef struct HrIntake HrIntake;
typedef struct {
    int kind; /* 0 no input, 1 claimed request, 2 valid seal, 3 control error */
    DWORD code;
    char request_id[37];
    wchar_t directory[PATH_CAP];
    unsigned char *plan;
    DWORD length;
} HrIntakeResult;

HrIntake *hr_intake_start(const wchar_t *directory, const char *nonce);
int hr_intake_schedule(HrIntake *intake);
/* -1 failure, 0 pending, 1 transfers result/plan ownership to caller. */
int hr_intake_poll(HrIntake *intake, HrIntakeResult *result);
void hr_intake_close(HrIntake *intake);
#endif
