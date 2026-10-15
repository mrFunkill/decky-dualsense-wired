/*
 * Reference dump for the wired-DualSense emulation: prints what Windows (or Wine) reports for
 * DualSense audio endpoints and HID devices — every property of matching endpoints and the
 * HID device tree — so Wine's view can be compared with real Windows.
 *
 *   dsinfo.exe [filter]      filter defaults to "054C" (Sony); "*" shows all endpoints
 */

#define COBJMACROS
#include <windows.h>
#include <initguid.h>
#include <mmdeviceapi.h>
#include <functiondiscoverykeys_devpkey.h>
#include <propvarutil.h>
#include <cfgmgr32.h>
#include <devpkey.h>
#include <hidclass.h>
#include <stdio.h>
#include <io.h>
#include <fcntl.h>

static void print_value(const PROPVARIANT *v)
{
    switch (v->vt)
    {
    case VT_LPWSTR: wprintf(L"\"%ls\"", v->pwszVal); break;
    case VT_UI4: wprintf(L"0x%lx", v->ulVal); break;
    case VT_I4: wprintf(L"%ld", v->lVal); break;
    case VT_BOOL: wprintf(L"%ls", v->boolVal ? L"true" : L"false"); break;
    case VT_CLSID:
    {
        WCHAR s[64];
        StringFromGUID2(v->puuid, s, ARRAYSIZE(s));
        wprintf(L"%ls", s);
        break;
    }
    case VT_BLOB:
    {
        ULONG i;
        wprintf(L"blob[%lu]", v->blob.cbSize);
        for (i = 0; i < v->blob.cbSize && i < 48; i++) wprintf(L" %02x", v->blob.pBlobData[i]);
        break;
    }
    case VT_VECTOR | VT_LPWSTR:
    {
        ULONG i;
        for (i = 0; i < v->calpwstr.cElems; i++) wprintf(L"[%ls] ", v->calpwstr.pElems[i]);
        break;
    }
    default: wprintf(L"(vt %u)", v->vt);
    }
}

static BOOL matches(const WCHAR *s, const WCHAR *filter)
{
    WCHAR up[1024];
    size_t i;
    if (!wcscmp(filter, L"*")) return TRUE;
    for (i = 0; s[i] && i < ARRAYSIZE(up) - 1; i++) up[i] = towupper(s[i]);
    up[i] = 0;
    return wcsstr(up, filter) != NULL;
}

static void dump_endpoints(const WCHAR *filter)
{
    IMMDeviceEnumerator *enumerator;
    IMMDeviceCollection *devices;
    UINT count, i;

    if (FAILED(CoCreateInstance(&CLSID_MMDeviceEnumerator, NULL, CLSCTX_ALL, &IID_IMMDeviceEnumerator,
                                (void **)&enumerator)))
        return;
    if (FAILED(IMMDeviceEnumerator_EnumAudioEndpoints(enumerator, eAll, DEVICE_STATE_ACTIVE, &devices))) return;
    IMMDeviceCollection_GetCount(devices, &count);
    wprintf(L"=== %u active audio endpoints\n", count);
    for (i = 0; i < count; i++)
    {
        IMMDevice *device;
        IPropertyStore *store;
        PROPVARIANT v;
        WCHAR *id = NULL;
        DWORD n, j;
        BOOL interesting = FALSE;

        IMMDeviceCollection_Item(devices, i, &device);
        IMMDevice_GetId(device, &id);
        IMMDevice_OpenPropertyStore(device, STGM_READ, &store);
        PropVariantInit(&v);
        if (SUCCEEDED(IPropertyStore_GetValue(store, &PKEY_Device_FriendlyName, &v)) && v.vt == VT_LPWSTR)
            interesting = matches(v.pwszVal, filter) || matches(v.pwszVal, L"WIRELESS CONTROLLER")
                          || matches(v.pwszVal, L"DUALSENSE");
        wprintf(L"\n[%u] %ls  name: ", i, id);
        print_value(&v);
        wprintf(L"\n");
        PropVariantClear(&v);
        IPropertyStore_GetCount(store, &n);
        for (j = 0; j < n; j++)
        {
            PROPERTYKEY key;
            WCHAR guid[64];
            IPropertyStore_GetAt(store, j, &key);
            PropVariantInit(&v);
            if (FAILED(IPropertyStore_GetValue(store, &key, &v))) continue;
            /* everything for DualSense-looking endpoints, the few keys the game reads otherwise */
            if (interesting || key.pid == 2 || key.pid == 3 || key.pid == 14)
            {
                StringFromGUID2(&key.fmtid, guid, ARRAYSIZE(guid));
                if (interesting || !wcscmp(guid, L"{B3F8FA53-0004-438E-9003-51A46E139BFC}")
                    || !wcscmp(guid, L"{1DA5D803-D492-4EDD-8C23-E0C0FFEE7F0E}")
                    || !wcscmp(guid, L"{8C7ED206-3F8A-4827-B3AB-AE9E1FAEFC6C}"))
                {
                    wprintf(L"    %ls,%lu = ", guid, key.pid);
                    print_value(&v);
                    wprintf(L"\n");
                }
            }
            PropVariantClear(&v);
        }
        IPropertyStore_Release(store);
        CoTaskMemFree(id);
        IMMDevice_Release(device);
    }
}

static void print_node(DEVINST node, int depth)
{
    WCHAR id[MAX_DEVICE_ID_LEN];
    BYTE buf[4096];
    ULONG size;
    DEVPROPTYPE type;
    DEVINST parent;

    if (CM_Get_Device_IDW(node, id, ARRAYSIZE(id), 0)) return;
    wprintf(L"%*ls%ls\n", depth * 2, L"", id);
    size = sizeof(buf);
    if (!CM_Get_DevNode_PropertyW(node, &DEVPKEY_Device_CompatibleIds, &type, buf, &size, 0))
    {
        WCHAR *s;
        wprintf(L"%*ls  compatible:", depth * 2, L"");
        for (s = (WCHAR *)buf; *s; s += wcslen(s) + 1) wprintf(L" [%ls]", s);
        wprintf(L"\n");
    }
    size = sizeof(buf);
    if (!CM_Get_DevNode_PropertyW(node, &DEVPKEY_Device_ContainerId, &type, buf, &size, 0))
    {
        WCHAR s[64];
        StringFromGUID2((GUID *)buf, s, ARRAYSIZE(s));
        wprintf(L"%*ls  container: %ls\n", depth * 2, L"", s);
    }
    size = sizeof(buf);
    if (!CM_Get_DevNode_PropertyW(node, &DEVPKEY_Device_Class, &type, buf, &size, 0))
        wprintf(L"%*ls  class: %ls\n", depth * 2, L"", (WCHAR *)buf);
    if (depth < 6 && !CM_Get_Parent(&parent, node, 0)) print_node(parent, depth + 1);
}

static void dump_hid(const WCHAR *filter)
{
    ULONG len = 0;
    WCHAR *list, *iface;
    if (CM_Get_Device_Interface_List_SizeW(&len, (GUID *)&GUID_DEVINTERFACE_HID, NULL, 0) || !len) return;
    list = calloc(len, sizeof(WCHAR));
    CM_Get_Device_Interface_ListW((GUID *)&GUID_DEVINTERFACE_HID, NULL, list, len, 0);
    wprintf(L"\n=== HID interfaces matching %ls\n", filter);
    for (iface = list; *iface; iface += wcslen(iface) + 1)
    {
        WCHAR instance[MAX_DEVICE_ID_LEN];
        ULONG size = sizeof(instance);
        DEVPROPTYPE type;
        DEVINST node;
        if (!matches(iface, filter)) continue;
        wprintf(L"\n%ls\n", iface);
        if (CM_Get_Device_Interface_PropertyW(iface, &DEVPKEY_Device_InstanceId, &type, (BYTE *)instance, &size, 0)) continue;
        if (!CM_Locate_DevNodeW(&node, instance, CM_LOCATE_DEVNODE_NORMAL)) print_node(node, 1);
    }
}

int wmain(int argc, WCHAR **argv)
{
    const WCHAR *filter = argc > 1 ? argv[1] : L"054C";
    _setmode(_fileno(stdout), _O_U8TEXT);
    CoInitializeEx(NULL, COINIT_MULTITHREADED);
    dump_endpoints(filter);
    dump_hid(!wcscmp(filter, L"*") ? L"054C" : filter);
    return 0;
}
