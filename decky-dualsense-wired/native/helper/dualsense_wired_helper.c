/*
 * DualSense Wired helper: a Wine service installed into Proton prefixes by the Decky plugin.
 *
 * On Windows every audio endpoint is also a device node, SWD\MMDEVAPI\<endpoint id>, with the
 * container ID of the hardware it belongs to. Games pair a controller with its audio device
 * through those nodes: they read SPDRP_BASE_CONTAINERID of every device and look for the
 * endpoint node in the HID device's container (seen with Spider-Man and a wired DualSense).
 * Wine does not create these nodes, so games never find the controller's speaker and HD
 * haptics. This service creates them from what Wine's mmdevapi reports (the container ID of
 * an endpoint is derived by winepulse from the same USB device winebus uses for the HID), and
 * keeps them current while devices come and go.
 */

#define COBJMACROS
#include <windows.h>
#include <initguid.h>
#include <mmdeviceapi.h>
#include <functiondiscoverykeys_devpkey.h>
#include <devpkey.h>
#include <stdio.h>
#include <wchar.h>

#define SERVICE_NAME L"DualSenseWiredHelper"

static const WCHAR enum_root[] = L"System\\CurrentControlSet\\Enum\\SWD\\MMDEVAPI\\";
static const WCHAR audio_endpoint_class_guid[] = L"{c166523c-fe0c-4a94-a586-f1a80cfbbf3e}";
/* Containers that do not identify a device (default "no container" GUIDs). */
static const GUID null_container = { 0 };
static const GUID none_container = { 0x00000000, 0x0000, 0x0000, { 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff } };

static SERVICE_STATUS_HANDLE status_handle;
static HANDLE stop_event, changed_event;

static void trace(const char *fmt, const WCHAR *a)
{
    char buf[512];
    _snprintf(buf, sizeof(buf) - 1, fmt, a);
    buf[sizeof(buf) - 1] = 0;
    OutputDebugStringA(buf);
}

static void set_string(HKEY key, const WCHAR *name, const WCHAR *value)
{
    RegSetValueExW(key, name, 0, REG_SZ, (const BYTE *)value, (DWORD)(wcslen(value) + 1) * sizeof(WCHAR));
}

static BOOL get_string(IPropertyStore *store, const PROPERTYKEY *key, WCHAR *out, size_t len)
{
    PROPVARIANT v;
    BOOL ok = FALSE;
    PropVariantInit(&v);
    if (SUCCEEDED(IPropertyStore_GetValue(store, key, &v)) && v.vt == VT_LPWSTR && v.pwszVal)
    {
        wcsncpy(out, v.pwszVal, len - 1);
        out[len - 1] = 0;
        ok = TRUE;
    }
    PropVariantClear(&v);
    return ok;
}

/* Create/refresh SWD\MMDEVAPI\<id> for one endpoint. */
static void register_endpoint(IMMDevice *device)
{
    static const WCHAR hardware_ids[] = L"MMDEVAPI\\AudioEndpoints\0";
    WCHAR *id = NULL, path[512], container[64], name[256], desc[256], driver[64];
    IPropertyStore *store = NULL;
    PROPVARIANT v;
    HKEY key;

    if (FAILED(IMMDevice_GetId(device, &id))) return;
    if (FAILED(IMMDevice_OpenPropertyStore(device, STGM_READ, &store))) goto done;
    PropVariantInit(&v);
    if (FAILED(IPropertyStore_GetValue(store, (const PROPERTYKEY *)&DEVPKEY_Device_ContainerId, &v))
        || v.vt != VT_CLSID || !v.puuid || IsEqualGUID(v.puuid, &null_container)
        || IsEqualGUID(v.puuid, &none_container))
    {
        PropVariantClear(&v);
        goto done;
    }
    StringFromGUID2(v.puuid, container, ARRAYSIZE(container));
    PropVariantClear(&v);
    if (!get_string(store, &PKEY_Device_FriendlyName, name, ARRAYSIZE(name))) name[0] = 0;
    if (!get_string(store, &PKEY_Device_DeviceDesc, desc, ARRAYSIZE(desc))) wcscpy(desc, name);

    _snwprintf(path, ARRAYSIZE(path), L"%ls%ls", enum_root, id);
    path[ARRAYSIZE(path) - 1] = 0;
    if (RegCreateKeyExW(HKEY_LOCAL_MACHINE, path, 0, NULL, 0, KEY_ALL_ACCESS, NULL, &key, NULL)) goto done;
    _snwprintf(driver, ARRAYSIZE(driver), L"%ls\\0000", audio_endpoint_class_guid);
    driver[ARRAYSIZE(driver) - 1] = 0;
    set_string(key, L"ContainerId", container);
    set_string(key, L"Class", L"AudioEndpoint");
    set_string(key, L"ClassGUID", audio_endpoint_class_guid);
    set_string(key, L"DeviceDesc", desc);
    set_string(key, L"FriendlyName", name);
    set_string(key, L"Driver", driver);
    RegSetValueExW(key, L"HardwareID", 0, REG_MULTI_SZ, (const BYTE *)hardware_ids, sizeof(hardware_ids));
    /* Marks nodes created by this helper (the plugin removes them on uninstall). */
    set_string(key, L"DualSenseWired", L"1");
    RegCloseKey(key);
    trace("dualsense_wired_helper: %ls\n", id);

done:
    if (store) IPropertyStore_Release(store);
    CoTaskMemFree(id);
}

static void sync_endpoints(IMMDeviceEnumerator *enumerator)
{
    IMMDeviceCollection *devices;
    UINT count, i;
    if (FAILED(IMMDeviceEnumerator_EnumAudioEndpoints(enumerator, eAll, DEVICE_STATE_ACTIVE, &devices))) return;
    if (SUCCEEDED(IMMDeviceCollection_GetCount(devices, &count)))
        for (i = 0; i < count; i++)
        {
            IMMDevice *device;
            if (SUCCEEDED(IMMDeviceCollection_Item(devices, i, &device)))
            {
                register_endpoint(device);
                IMMDevice_Release(device);
            }
        }
    IMMDeviceCollection_Release(devices);
}

/* --- endpoint change notifications: just signal the main loop --- */

static HRESULT WINAPI notify_QueryInterface(IMMNotificationClient *iface, REFIID riid, void **out)
{
    if (IsEqualIID(riid, &IID_IUnknown) || IsEqualIID(riid, &IID_IMMNotificationClient))
    {
        *out = iface;
        return S_OK;
    }
    *out = NULL;
    return E_NOINTERFACE;
}
static ULONG WINAPI notify_AddRef(IMMNotificationClient *iface) { return 2; }
static ULONG WINAPI notify_Release(IMMNotificationClient *iface) { return 1; }
static HRESULT WINAPI notify_changed(IMMNotificationClient *iface) { SetEvent(changed_event); return S_OK; }
static HRESULT WINAPI notify_state(IMMNotificationClient *iface, LPCWSTR id, DWORD state) { return notify_changed(iface); }
static HRESULT WINAPI notify_added(IMMNotificationClient *iface, LPCWSTR id) { return notify_changed(iface); }
static HRESULT WINAPI notify_removed(IMMNotificationClient *iface, LPCWSTR id) { return notify_changed(iface); }
static HRESULT WINAPI notify_default(IMMNotificationClient *iface, EDataFlow flow, ERole role, LPCWSTR id) { return S_OK; }
static HRESULT WINAPI notify_property(IMMNotificationClient *iface, LPCWSTR id, const PROPERTYKEY key) { return S_OK; }

static IMMNotificationClientVtbl notify_vtbl = {
    notify_QueryInterface, notify_AddRef, notify_Release,
    notify_state, notify_added, notify_removed, notify_default, notify_property,
};
static IMMNotificationClient notify_client = { &notify_vtbl };

static void run(void)
{
    IMMDeviceEnumerator *enumerator = NULL;
    HANDLE events[2];

    CoInitializeEx(NULL, COINIT_MULTITHREADED);
    if (FAILED(CoCreateInstance(&CLSID_MMDeviceEnumerator, NULL, CLSCTX_ALL, &IID_IMMDeviceEnumerator,
                                (void **)&enumerator)))
        goto done;
    sync_endpoints(enumerator);
    IMMDeviceEnumerator_RegisterEndpointNotificationCallback(enumerator, &notify_client);
    events[0] = stop_event;
    events[1] = changed_event;
    while (WaitForMultipleObjects(2, events, FALSE, INFINITE) == WAIT_OBJECT_0 + 1)
    {
        Sleep(500);   /* let a burst of notifications settle */
        ResetEvent(changed_event);
        sync_endpoints(enumerator);
    }
    IMMDeviceEnumerator_UnregisterEndpointNotificationCallback(enumerator, &notify_client);
    IMMDeviceEnumerator_Release(enumerator);
done:
    CoUninitialize();
}

/* --- service plumbing --- */

static void report(DWORD state)
{
    SERVICE_STATUS status = { SERVICE_WIN32_OWN_PROCESS, state,
                              state == SERVICE_RUNNING ? SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_SHUTDOWN : 0 };
    SetServiceStatus(status_handle, &status);
}

static DWORD WINAPI control_handler(DWORD control, DWORD type, void *data, void *context)
{
    if (control == SERVICE_CONTROL_STOP || control == SERVICE_CONTROL_SHUTDOWN)
    {
        report(SERVICE_STOP_PENDING);
        SetEvent(stop_event);
    }
    return NO_ERROR;
}

static void WINAPI service_main(DWORD argc, WCHAR **argv)
{
    status_handle = RegisterServiceCtrlHandlerExW(SERVICE_NAME, control_handler, NULL);
    if (!status_handle) return;
    /* Report running first: services.exe waits for it before starting other services. */
    report(SERVICE_RUNNING);
    run();
    report(SERVICE_STOPPED);
}

int wmain(int argc, WCHAR **argv)
{
    static const SERVICE_TABLE_ENTRYW table[] = { { (WCHAR *)SERVICE_NAME, service_main }, { NULL, NULL } };
    stop_event = CreateEventW(NULL, TRUE, FALSE, NULL);
    changed_event = CreateEventW(NULL, TRUE, FALSE, NULL);
    if (argc > 1 && !wcscmp(argv[1], L"--once"))
    {
        /* Manual run: sync once and exit. */
        SetEvent(stop_event);
        run();
        return 0;
    }
    return StartServiceCtrlDispatcherW(table) ? 0 : 1;
}
