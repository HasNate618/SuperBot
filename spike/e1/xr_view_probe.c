// Throwaway E1 spike OpenXR client.
//
// Creates a headless XR_MND_headless session (no graphics binding) and prints
// the head pose (VIEW space relative to LOCAL) at ~30 Hz. If the remote
// driver accepts our scripted head pose, yaw will oscillate.
//
// Build: see run_e1.sh / results markdown.
#define XR_NO_PROTOTYPES
#define XR_USE_TIMESPEC 1
#include <time.h>

#include <openxr/openxr.h>
#include <openxr/openxr_platform.h>

#include <dlfcn.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifndef XrStructureTypeInit
#define XrStructureTypeInit(x) (x)
#endif

static void *g_loader;
static XrInstance g_instance = XR_NULL_HANDLE;

#define LOAD_FN(name) PFN_##name p_##name = (PFN_##name)dlsym(g_loader, #name)
#define CHECK_XR(call, what)                                                                                           \
	do {                                                                                                           \
		XrResult _r = (call);                                                                                  \
		if (XR_FAILED(_r)) {                                                                                   \
			fprintf(stderr, "XR FAIL %s: %d\n", what, _r);                                                 \
			exit(1);                                                                                       \
		}                                                                                                      \
	} while (0)

static double
monotonic_seconds(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9;
}

/* Yaw (rotation about +Y) from quaternion, radians. */
static double
yaw_from_quat(XrQuaternionf q)
{
	return atan2(2.0 * (q.w * q.y + q.x * q.z), 1.0 - 2.0 * (q.y * q.y + q.z * q.z));
}

int
main(int argc, char **argv)
{
	double duration = argc > 1 ? atof(argv[1]) : 15.0;

	g_loader = dlopen("libopenxr_loader.so", RTLD_NOW);
	if (!g_loader) {
		fprintf(stderr, "dlopen(libopenxr_loader.so): %s\n", dlerror());
		return 1;
	}

	LOAD_FN(xrCreateInstance);
	LOAD_FN(xrDestroyInstance);
	LOAD_FN(xrGetSystem);
	LOAD_FN(xrGetInstanceProcAddr);
	LOAD_FN(xrCreateSession);
	LOAD_FN(xrDestroySession);
	LOAD_FN(xrCreateReferenceSpace);
	LOAD_FN(xrDestroySpace);
	LOAD_FN(xrLocateViews);
	LOAD_FN(xrPollEvent);
	LOAD_FN(xrBeginSession);
	LOAD_FN(xrEndSession);
	PFN_xrConvertTimespecTimeToTimeKHR p_xrConvertTimespecTimeToTimeKHR = NULL;

	XrInstanceCreateInfo ici = {.type = XrStructureTypeInit(XR_TYPE_INSTANCE_CREATE_INFO)};
	strcpy(ici.applicationInfo.applicationName, "e1-xr-view-probe");
	strcpy(ici.applicationInfo.engineName, "e1-spike");
	ici.applicationInfo.apiVersion = XR_API_VERSION_1_0;
	const char *exts[] = {XR_MND_HEADLESS_EXTENSION_NAME, XR_KHR_CONVERT_TIMESPEC_TIME_EXTENSION_NAME};
	ici.enabledExtensionNames = exts;
	ici.enabledExtensionCount = 2;

	CHECK_XR(p_xrCreateInstance(&ici, &g_instance), "xrCreateInstance");

	PFN_xrVoidFunction fn = NULL;
	CHECK_XR(p_xrGetInstanceProcAddr(g_instance, "xrConvertTimespecTimeToTimeKHR", &fn),
	         "xrGetInstanceProcAddr(xrConvertTimespecTimeToTimeKHR)");
	p_xrConvertTimespecTimeToTimeKHR = (PFN_xrConvertTimespecTimeToTimeKHR)fn;
	if (p_xrConvertTimespecTimeToTimeKHR == NULL) {
		fprintf(stderr, "xrConvertTimespecTimeToTimeKHR unavailable\n");
		return 1;
	}

	XrSystemGetInfo sgi = {.type = XrStructureTypeInit(XR_TYPE_SYSTEM_GET_INFO),
	                       .formFactor = XR_FORM_FACTOR_HEAD_MOUNTED_DISPLAY};
	XrSystemId sysid = XR_NULL_SYSTEM_ID;
	CHECK_XR(p_xrGetSystem(g_instance, &sgi, &sysid), "xrGetSystem");

	XrSessionCreateInfo sci = {.type = XrStructureTypeInit(XR_TYPE_SESSION_CREATE_INFO),
	                           .systemId = sysid,
	                           .next = NULL};
	XrSession session = XR_NULL_HANDLE;
	CHECK_XR(p_xrCreateSession(g_instance, &sci, &session), "xrCreateSession (headless)");

	XrReferenceSpaceCreateInfo rsci = {.type = XrStructureTypeInit(XR_TYPE_REFERENCE_SPACE_CREATE_INFO),
	                                   .referenceSpaceType = XR_REFERENCE_SPACE_TYPE_LOCAL,
	                                   .poseInReferenceSpace = {{0, 0, 0, 1}, {0, 0, 0}}};
	XrSpace local_space = XR_NULL_HANDLE;
	CHECK_XR(p_xrCreateReferenceSpace(session, &rsci, &local_space), "xrCreateReferenceSpace");

	printf("session created, polling views ...\n");
	fflush(stdout);

	int begun = 0;
	double t0 = monotonic_seconds();
	double last_print = -1.0;
	int frames = 0;

	while (monotonic_seconds() - t0 < duration) {
		XrEventDataBuffer ev = {.type = XrStructureTypeInit(XR_TYPE_EVENT_DATA_BUFFER)};
		while (p_xrPollEvent(g_instance, &ev) == XR_SUCCESS) {
			if (ev.type == XR_TYPE_EVENT_DATA_SESSION_STATE_CHANGED) {
				XrEventDataSessionStateChanged *ss = (XrEventDataSessionStateChanged *)&ev;
				if (ss->state == XR_SESSION_STATE_READY && !begun) {
					XrSessionBeginInfo sbi = {
					    .type = XrStructureTypeInit(XR_TYPE_SESSION_BEGIN_INFO),
					    .primaryViewConfigurationType = XR_VIEW_CONFIGURATION_TYPE_PRIMARY_STEREO};
					CHECK_XR(p_xrBeginSession(session, &sbi), "xrBeginSession");
					begun = 1;
					printf("session begun\n");
					fflush(stdout);
				}
			}
			ev = (XrEventDataBuffer){.type = XrStructureTypeInit(XR_TYPE_EVENT_DATA_BUFFER)};
		}

		struct timespec ts;
		clock_gettime(CLOCK_MONOTONIC, &ts);
		XrTime display_time = 0;
		CHECK_XR(p_xrConvertTimespecTimeToTimeKHR(g_instance, &ts, &display_time),
		         "xrConvertTimespecTimeToTimeKHR");

		XrViewLocateInfo vli = {.type = XrStructureTypeInit(XR_TYPE_VIEW_LOCATE_INFO),
		                        .viewConfigurationType = XR_VIEW_CONFIGURATION_TYPE_PRIMARY_STEREO,
		                        .displayTime = display_time,
		                        .space = local_space};
		XrViewState vs = {.type = XrStructureTypeInit(XR_TYPE_VIEW_STATE)};
		XrView views[2] = {{.type = XrStructureTypeInit(XR_TYPE_VIEW)},
		                   {.type = XrStructureTypeInit(XR_TYPE_VIEW)}};
		uint32_t view_count = 0;
		XrResult lr = p_xrLocateViews(session, &vli, &vs, 2, &view_count, views);
		if (XR_FAILED(lr)) {
			fprintf(stderr, "xrLocateViews failed: %d\n", lr);
			break;
		}

		struct timespec nap = {.tv_sec = 0, .tv_nsec = 33000000};
		nanosleep(&nap, NULL);

		double now = monotonic_seconds();
		if (now - last_print >= 0.2) {
			XrQuaternionf q = views[0].pose.orientation;
			XrVector3f p = views[0].pose.position;
			int valid = (vs.viewStateFlags & XR_VIEW_STATE_ORIENTATION_VALID_BIT) != 0;
			printf("t=%6.2f frame=%5d views=%u valid=%d yaw=%+8.2f deg "
			       "q=(%+.4f,%+.4f,%+.4f,%+.4f) pos=(%+.3f,%+.3f,%+.3f)\n",
			       now - t0, frames, view_count, valid, yaw_from_quat(q) * 180.0 / M_PI, q.x, q.y, q.z,
			       q.w, p.x, p.y, p.z);
			fflush(stdout);
			last_print = now;
		}
		frames++;
	}

	p_xrDestroySpace(local_space);
	p_xrDestroySession(session);
	p_xrDestroyInstance(g_instance);
	printf("done, %d frames\n", frames);
	return 0;
}
