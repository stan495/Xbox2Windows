; coverage: source cpu_aot_ssse3_int
; Source-owned differential fixture for 14 baseline 128-bit SSSE3 packed integer ALU and shuffle instructions.
.code

pabsb_op PROC FRAME
    .endprolog
    movdqu xmm0, xmmword ptr [rcx]
    movdqu xmm1, xmmword ptr [rdx]
    pabsb xmm0, xmm1
    movdqu xmmword ptr [r8], xmm0
    ret
pabsb_op ENDP

pabsw_op PROC FRAME
    .endprolog
    movdqu xmm0, xmmword ptr [rcx]
    movdqu xmm1, xmmword ptr [rdx]
    pabsw xmm0, xmm1
    movdqu xmmword ptr [r8], xmm0
    ret
pabsw_op ENDP

pabsd_op PROC FRAME
    .endprolog
    movdqu xmm0, xmmword ptr [rcx]
    movdqu xmm1, xmmword ptr [rdx]
    pabsd xmm0, xmm1
    movdqu xmmword ptr [r8], xmm0
    ret
pabsd_op ENDP

psignb_op PROC FRAME
    .endprolog
    movdqu xmm0, xmmword ptr [rcx]
    movdqu xmm1, xmmword ptr [rdx]
    psignb xmm0, xmm1
    movdqu xmmword ptr [r8], xmm0
    ret
psignb_op ENDP

psignw_op PROC FRAME
    .endprolog
    movdqu xmm0, xmmword ptr [rcx]
    movdqu xmm1, xmmword ptr [rdx]
    psignw xmm0, xmm1
    movdqu xmmword ptr [r8], xmm0
    ret
psignw_op ENDP

psignd_op PROC FRAME
    .endprolog
    movdqu xmm0, xmmword ptr [rcx]
    movdqu xmm1, xmmword ptr [rdx]
    psignd xmm0, xmm1
    movdqu xmmword ptr [r8], xmm0
    ret
psignd_op ENDP

palignr_op PROC FRAME
    .endprolog
    movdqu xmm0, xmmword ptr [rcx]
    movdqu xmm1, xmmword ptr [rdx]
    palignr xmm0, xmm1, 5
    movdqu xmmword ptr [r8], xmm0
    ret
palignr_op ENDP

pshufb_op PROC FRAME
    .endprolog
    movdqu xmm0, xmmword ptr [rcx]
    movdqu xmm1, xmmword ptr [rdx]
    pshufb xmm0, xmm1
    movdqu xmmword ptr [r8], xmm0
    ret
pshufb_op ENDP

phaddw_op PROC FRAME
    .endprolog
    movdqu xmm0, xmmword ptr [rcx]
    movdqu xmm1, xmmword ptr [rdx]
    phaddw xmm0, xmm1
    movdqu xmmword ptr [r8], xmm0
    ret
phaddw_op ENDP

phaddd_op PROC FRAME
    .endprolog
    movdqu xmm0, xmmword ptr [rcx]
    movdqu xmm1, xmmword ptr [rdx]
    phaddd xmm0, xmm1
    movdqu xmmword ptr [r8], xmm0
    ret
phaddd_op ENDP

phaddsw_op PROC FRAME
    .endprolog
    movdqu xmm0, xmmword ptr [rcx]
    movdqu xmm1, xmmword ptr [rdx]
    phaddsw xmm0, xmm1
    movdqu xmmword ptr [r8], xmm0
    ret
phaddsw_op ENDP

phsubw_op PROC FRAME
    .endprolog
    movdqu xmm0, xmmword ptr [rcx]
    movdqu xmm1, xmmword ptr [rdx]
    phsubw xmm0, xmm1
    movdqu xmmword ptr [r8], xmm0
    ret
phsubw_op ENDP

phsubd_op PROC FRAME
    .endprolog
    movdqu xmm0, xmmword ptr [rcx]
    movdqu xmm1, xmmword ptr [rdx]
    phsubd xmm0, xmm1
    movdqu xmmword ptr [r8], xmm0
    ret
phsubd_op ENDP

phsubsw_op PROC FRAME
    .endprolog
    movdqu xmm0, xmmword ptr [rcx]
    movdqu xmm1, xmmword ptr [rdx]
    phsubsw xmm0, xmm1
    movdqu xmmword ptr [r8], xmm0
    ret
phsubsw_op ENDP

END
