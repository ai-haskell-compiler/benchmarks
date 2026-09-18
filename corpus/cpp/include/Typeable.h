/*
 * A stand-in for the Typeable.h that base ships, for the benchmark corpus
 * only.
 *
 * Old code includes it for INSTANCE_TYPEABLE macros that expand to a
 * deriving clause. Modern GHC derives Typeable for every type, so the real
 * header's macros expand to nothing, and so do these. Written from the macro
 * names the real header defines, not copied from it.
 */

#pragma once

#define INSTANCE_TYPEABLE0(tycon)
#define INSTANCE_TYPEABLE1(tycon)
#define INSTANCE_TYPEABLE2(tycon)
#define INSTANCE_TYPEABLE3(tycon)
#define INSTANCE_TYPEABLE4(tycon)
#define INSTANCE_TYPEABLE5(tycon)
#define INSTANCE_TYPEABLE6(tycon)
#define INSTANCE_TYPEABLE7(tycon)
