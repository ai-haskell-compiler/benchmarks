/*
 * A stand-in for the ghcautoconf.h that GHC's configure script generates,
 * for the benchmark corpus only.
 *
 * The real header is several hundred feature-test macros. The corpus only
 * needs the ones that decide which branch a module takes, so a conventional
 * 64-bit Linux is described and nothing else is claimed. Written from the
 * macro names the real header defines, not copied from it.
 */

#pragma once

#define SIZEOF_VOID_P 8
#define SIZEOF_LONG 8
#define SIZEOF_INT 4
#define SIZEOF_SHORT 2
#define SIZEOF_CHAR 1
#define SIZEOF_LONG_LONG 8
#define SIZEOF_DOUBLE 8
#define SIZEOF_FLOAT 4

#define HAVE_UNISTD_H 1
#define HAVE_SYS_TYPES_H 1
#define HAVE_SYS_STAT_H 1
#define HAVE_SYS_TIME_H 1
#define HAVE_SIGNAL_H 1
#define HAVE_TERMIOS_H 1
#define HAVE_FCNTL_H 1
#define HAVE_ERRNO_H 1
#define HAVE_PTHREAD_H 1
#define HAVE_LIBPTHREAD 1
#define HAVE_GETTIMEOFDAY 1
#define HAVE_CLOCK_GETTIME 1
#define HAVE_EVENTFD 1
#define HAVE_EPOLL_CTL 1

#define WORDS_BIGENDIAN 0
#define FLOAT_WORDS_BIGENDIAN 0
