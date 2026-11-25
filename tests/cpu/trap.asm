; coverage: record cpu_aot_trap
; Source-owned differential fixture for INT3 (CC), UD2 (0F 0B) and UD1 (0F B9 /r) in the shared lowering. Each
; export takes (a, b, c, state) in RCX, RDX, R8 and R9. state[0] is the store before the trap and state[1] the guest
; work after it, which must not run when the trap unwinds to the caller. trap_branch reaches INT3 only when a is
; nonzero. ML64 has no UD1 spelling, so it is written as bytes; trap_ud1_mem and trap_ud1_disp get an inaccessible
; page in RCX.
.code

trap_int3 PROC FRAME
    .endprolog
    mov qword ptr [r9], 1
    int 3
    mov qword ptr [r9+8], 2
    mov eax, 7
    ret
trap_int3 ENDP

trap_ud2 PROC FRAME
    .endprolog
    mov qword ptr [r9], 1
    ud2
    mov qword ptr [r9+8], 2
    mov eax, 7
    ret
trap_ud2 ENDP

trap_ud1_reg PROC FRAME
    .endprolog
    mov qword ptr [r9], 1
    db 0Fh, 0B9h, 0C0h              ; ud1 eax, eax
    mov qword ptr [r9+8], 2
    mov eax, 7
    ret
trap_ud1_reg ENDP

trap_ud1_mem PROC FRAME
    .endprolog
    mov qword ptr [r9], 1
    db 0Fh, 0B9h, 01h               ; ud1 eax, dword ptr [rcx]
    mov qword ptr [r9+8], 2
    mov eax, 7
    ret
trap_ud1_mem ENDP

trap_ud1_disp PROC FRAME
    .endprolog
    mov qword ptr [r9], 1
    db 0Fh, 0B9h, 81h, 00h, 01h, 00h, 00h ; ud1 eax, dword ptr [rcx+100h]
    mov qword ptr [r9+8], 2
    mov eax, 7
    ret
trap_ud1_disp ENDP

trap_branch PROC FRAME
    .endprolog
    mov qword ptr [r9], 1
    test ecx, ecx
    jz skip
    int 3
skip:
    mov qword ptr [r9+8], 2
    mov eax, 7
    ret
trap_branch ENDP

END
