/*
 * A stand-in for GHC's MachDeps.h, for the benchmark corpus only. Taken from
 * the aihc-cpp repository, whose sweep over the same snapshot found this to be
 * the GHC header the corpus needs most; both repositories are public domain.
 *
 * Around a hundred modules in a Stackage snapshot include this header. In a
 * real build GHC supplies it from the RTS include directory; a bare source tree
 * has no such directory, so those modules fail to preprocess and drop out of
 * the measurement — and the tools disagree about whether that is fatal, which
 * made the failure counts describe include resolution rather than the
 * preprocessors.
 *
 * The values below describe a conventional 64-bit platform. They are not read
 * for accuracy: nothing here is compiled, only preprocessed, so all that
 * matters is that the macros are defined and that arithmetic in #if conditions
 * evaluates. This is written from the macro names GHC's header defines rather
 * than copied from it, so that this repository stays under a single licence.
 */

#pragma once

#define WORD_SIZE_IN_BITS 64
#define WORD_SIZE_IN_BITS_FLOAT 64

#define SIZEOF_HSCHAR 4
#define ALIGNMENT_HSCHAR 4
#define SIZEOF_HSINT 8
#define ALIGNMENT_HSINT 8
#define SIZEOF_HSWORD 8
#define ALIGNMENT_HSWORD 8
#define SIZEOF_HSFLOAT 4
#define ALIGNMENT_HSFLOAT 4
#define SIZEOF_HSDOUBLE 8
#define ALIGNMENT_HSDOUBLE 8
#define SIZEOF_HSPTR 8
#define ALIGNMENT_HSPTR 8
#define SIZEOF_HSFUNPTR 8
#define ALIGNMENT_HSFUNPTR 8
#define SIZEOF_HSSTABLEPTR 8
#define ALIGNMENT_HSSTABLEPTR 8

#define SIZEOF_INT8 1
#define ALIGNMENT_INT8 1
#define SIZEOF_WORD8 1
#define ALIGNMENT_WORD8 1
#define SIZEOF_INT16 2
#define ALIGNMENT_INT16 2
#define SIZEOF_WORD16 2
#define ALIGNMENT_WORD16 2
#define SIZEOF_INT32 4
#define ALIGNMENT_INT32 4
#define SIZEOF_WORD32 4
#define ALIGNMENT_WORD32 4
#define SIZEOF_INT64 8
#define ALIGNMENT_INT64 8
#define SIZEOF_WORD64 8
#define ALIGNMENT_WORD64 8

#define SIZEOF_VOID_P 8
#define ALIGNMENT_VOID_P 8
#define SIZEOF_LONG 8
#define ALIGNMENT_LONG 8
#define SIZEOF_UNSIGNED_LONG 8
#define ALIGNMENT_UNSIGNED_LONG 8

#define TAG_BITS 3
#define TAG_MASK ((1 << TAG_BITS) - 1)
