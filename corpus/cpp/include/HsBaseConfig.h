/*
 * A stand-in for the HsBaseConfig.h that GHC ships with base, for the
 * benchmark corpus only. Taken from the aihc-cpp repository, whose sweep over
 * the same snapshot found the HTYPE_ family to be what the corpus uses; both
 * repositories are public domain.
 *
 * Eighteen modules in a Stackage snapshot include this header. The real one is
 * around nine hundred lines, almost all of it errno constants that no module in
 * the corpus refers to; what they do use is the HTYPE_ family, which names the
 * Haskell type corresponding to a C typedef and is substituted into type
 * declarations. Only those are defined here.
 *
 * The mapping below describes a conventional 64-bit Unix. Nothing is compiled,
 * only preprocessed, so the values need only be defined and expand to something
 * shaped like a type name. Written from the macro names the real header
 * defines rather than copied from it, so that this repository stays under a
 * single licence.
 */

#pragma once

/* C scalar types. */
#define HTYPE_CHAR Int8
#define HTYPE_SIGNED_CHAR Int8
#define HTYPE_UNSIGNED_CHAR Word8
#define HTYPE_SHORT Int16
#define HTYPE_UNSIGNED_SHORT Word16
#define HTYPE_INT Int32
#define HTYPE_UNSIGNED_INT Word32
#define HTYPE_LONG Int64
#define HTYPE_UNSIGNED_LONG Word64
#define HTYPE_LONG_LONG Int64
#define HTYPE_UNSIGNED_LONG_LONG Word64
#define HTYPE_FLOAT Float
#define HTYPE_DOUBLE Double
#define HTYPE_WCHAR_T Int32

/* <stdint.h> and <stddef.h> typedefs. */
#define HTYPE_SIZE_T Word64
#define HTYPE_PTRDIFF_T Int64
#define HTYPE_INTPTR_T Int64
#define HTYPE_UINTPTR_T Word64
#define HTYPE_INTMAX_T Int64
#define HTYPE_UINTMAX_T Word64
#define HTYPE_SIG_ATOMIC_T Int32

/* POSIX typedefs. */
#define HTYPE_DEV_T Word64
#define HTYPE_INO_T Word64
#define HTYPE_MODE_T Word32
#define HTYPE_OFF_T Int64
#define HTYPE_PID_T Int32
#define HTYPE_NLINK_T Word64
#define HTYPE_UID_T Word32
#define HTYPE_GID_T Word32
#define HTYPE_SSIZE_T Int64
#define HTYPE_ID_T Word32
#define HTYPE_KEY_T Int32
#define HTYPE_BLKSIZE_T Int64
#define HTYPE_BLKCNT_T Int64
#define HTYPE_FSBLKCNT_T Word64
#define HTYPE_FSFILCNT_T Word64
#define HTYPE_RLIM_T Word64
#define HTYPE_CLOCKID_T Int32
#define HTYPE_TIMER_T Word64
#define HTYPE_TIME_T Int64
#define HTYPE_CLOCK_T Int64
#define HTYPE_USECONDS_T Word32
#define HTYPE_SUSECONDS_T Int64
#define HTYPE_NLINK_T_SIGNED 0

/* Terminal control typedefs. */
#define HTYPE_CC_T Word8
#define HTYPE_SPEED_T Word64
#define HTYPE_TCFLAG_T Word64

/* Feature flags the corpus checks for. */
#define HAVE_UNISTD_H 1
#define HAVE_SYS_TYPES_H 1
#define HAVE_SYS_STAT_H 1
#define HAVE_TERMIOS_H 1
#define HAVE_SIGNAL_H 1
