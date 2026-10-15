/*
 * cfgmgr32 proxy for Proton. Forwards every export to Proton's own cfgmgr32 (copied next to
 * this DLL as cfgmgr32_wine.dll, see build.py) except the few below.
 *
 * Why: games find out how a controller is connected by walking the device tree from its HID
 * device upwards. hidapi on Windows does: HID device -> CM_Get_Parent -> parent devnode, and
 * the bus is USB if the parent's compatible IDs contain "USB". In Wine CM_Get_Parent is a
 * stub, so every controller is on an unknown bus and games never use the USB-only features
 * of a DualSense: its audio device (speaker, headset jack, HD haptics).
 *
 * winebus creates each HID device as  HID\VID_v&PID_p[&MI_nn]\<id>  with a parent PDO named
 * after the bus it found the device on:  USB\...\<id>  (or BTHENUM, WINEBUS, ...) with the
 * same hardware part and instance ID. So:
 *   CM_Get_Parent(HID\X\I)           -> the existing <bus>\X\I
 *   CM_Get_Parent(USB\X&MI_nn\I)      -> USB\X\I if it exists (composite device)
 * The USB PDOs carry the compatible ID WINEBUS\WINE_COMP_HID; on Windows a USB HID interface
 * has USB\Class_03&SubClass_00&Prot_00 etc., which is what we report for them instead.
 */

#include <windows.h>
#include <initguid.h>
#include <cfgmgr32.h>
#include <devpkey.h>
#include <setupapi.h>
#include <stdio.h>
#include <wchar.h>

typedef CONFIGRET (WINAPI *get_device_id_fn)(DEVINST, PWSTR, ULONG, ULONG);
typedef CONFIGRET (WINAPI *locate_devnode_fn)(PDEVINST, DEVINSTID_W, ULONG);
typedef CONFIGRET (WINAPI *get_property_fn)(DEVINST, const DEVPROPKEY *, DEVPROPTYPE *, PBYTE,
                                            PULONG, ULONG, HMACHINE);
typedef CONFIGRET (WINAPI *get_registry_property_fn)(DEVINST, ULONG, PULONG, PVOID, PULONG,
                                                     ULONG, HMACHINE);

static get_device_id_fn p_CM_Get_Device_IDW;
static locate_devnode_fn p_CM_Locate_DevNodeW;
static get_property_fn p_CM_Get_DevNode_Property_ExW;
static get_registry_property_fn p_CM_Get_DevNode_Registry_Property_ExW;

/* REG_MULTI_SZ, as on Windows for a USB HID interface. */
static const WCHAR usb_hid_compatible_ids[] =
    L"USB\\Class_03&SubClass_00&Prot_00\0USB\\Class_03&SubClass_00\0USB\\Class_03\0";

static void debug(const char *fmt, const WCHAR *a, const WCHAR *b)
{
    char buf[1024];
    _snprintf(buf, sizeof(buf) - 1, fmt, a, b);
    buf[sizeof(buf) - 1] = 0;
    OutputDebugStringA(buf);
}

static BOOL resolve(void)
{
    static BOOL done;
    HMODULE wine;
    if (done) return p_CM_Get_Device_IDW != NULL;
    wine = LoadLibraryW(L"cfgmgr32_wine.dll");
    if (wine)
    {
        p_CM_Get_Device_IDW = (get_device_id_fn)GetProcAddress(wine, "CM_Get_Device_IDW");
        p_CM_Locate_DevNodeW = (locate_devnode_fn)GetProcAddress(wine, "CM_Locate_DevNodeW");
        p_CM_Get_DevNode_Property_ExW = (get_property_fn)GetProcAddress(wine, "CM_Get_DevNode_Property_ExW");
        p_CM_Get_DevNode_Registry_Property_ExW =
            (get_registry_property_fn)GetProcAddress(wine, "CM_Get_DevNode_Registry_Property_ExW");
    }
    done = TRUE;
    if (!p_CM_Get_Device_IDW || !p_CM_Locate_DevNodeW || !p_CM_Get_DevNode_Property_ExW
        || !p_CM_Get_DevNode_Registry_Property_ExW)
        p_CM_Get_Device_IDW = NULL;
    return p_CM_Get_Device_IDW != NULL;
}

/* "A\B\C" -> enumerator "A", hardware part "B", instance "C" (pointers into id). */
static BOOL split_id(WCHAR *id, WCHAR **enumerator, WCHAR **hardware, WCHAR **instance)
{
    WCHAR *s1 = wcschr(id, '\\'), *s2;
    if (!s1 || !(s2 = wcschr(s1 + 1, '\\'))) return FALSE;
    *s1 = *s2 = 0;
    *enumerator = id;
    *hardware = s1 + 1;
    *instance = s2 + 1;
    return TRUE;
}

static CONFIGRET locate(DEVINST *node, const WCHAR *enumerator, const WCHAR *hardware,
                        const WCHAR *instance, WCHAR *id, size_t id_len)
{
    /* Wine only knows devnodes of devices this process has already opened through SetupAPI
     * (they are registered when the device object is created), so open it if needed. The
     * device info set is kept for the life of the process to keep those devnodes valid. */
    static HDEVINFO set = INVALID_HANDLE_VALUE;
    SP_DEVINFO_DATA data = { sizeof(data) };

    if (_snwprintf(id, id_len, L"%ls\\%ls\\%ls", enumerator, hardware, instance) < 0)
        return CR_BUFFER_SMALL;
    if (!p_CM_Locate_DevNodeW(node, id, CM_LOCATE_DEVNODE_NORMAL)) return CR_SUCCESS;
    if (set == INVALID_HANDLE_VALUE) set = SetupDiCreateDeviceInfoList(NULL, NULL);
    if (set == INVALID_HANDLE_VALUE || !SetupDiOpenDeviceInfoW(set, id, NULL, 0, &data))
        return CR_NO_SUCH_DEVNODE;
    *node = data.DevInst;
    return CR_SUCCESS;
}

/* The devnode is a USB device as winebus names it (its enumerator is "USB"). */
static BOOL is_winebus_usb_node(DEVINST node)
{
    WCHAR id[MAX_DEVICE_ID_LEN];
    return !p_CM_Get_Device_IDW(node, id, ARRAYSIZE(id), 0) && !_wcsnicmp(id, L"USB\\", 4);
}

static BOOL multi_sz_mentions_usb(const WCHAR *s)
{
    for (; *s; s += wcslen(s) + 1)
        if (wcsstr(s, L"USB")) return TRUE;
    return FALSE;
}

CONFIGRET WINAPI CM_Get_Parent_Ex(PDEVINST parent, DEVINST child, ULONG flags, HMACHINE machine)
{
    static const WCHAR *buses[] = { L"USB", L"BTHENUM", L"BTHLEDEVICE", L"WINEBUS" };
    WCHAR child_id[MAX_DEVICE_ID_LEN], parent_id[MAX_DEVICE_ID_LEN], *enumerator, *hardware, *instance, *mi;
    CONFIGRET ret;
    int i;

    if (!parent) return CR_INVALID_POINTER;
    *parent = 0;
    if (!resolve()) return CR_FAILURE;
    if ((ret = p_CM_Get_Device_IDW(child, child_id, ARRAYSIZE(child_id), 0))) return ret;
    debug("cfgmgr32 shim: CM_Get_Parent(%ls)%ls\n", child_id, L"");
    if (!split_id(child_id, &enumerator, &hardware, &instance)) return CR_NO_SUCH_DEVNODE;

    if (!_wcsicmp(enumerator, L"HID"))
    {
        for (i = 0; i < ARRAYSIZE(buses); i++)
            if (!locate(parent, buses[i], hardware, instance, parent_id, ARRAYSIZE(parent_id)))
            {
                debug("cfgmgr32 shim:   -> %ls%ls\n", parent_id, L"");
                return CR_SUCCESS;
            }
    }
    else if (!_wcsicmp(enumerator, L"USB") && (mi = wcsstr(hardware, L"&MI_")))
    {
        *mi = 0;    /* interface of a composite device -> the device */
        if (!locate(parent, L"USB", hardware, instance, parent_id, ARRAYSIZE(parent_id)))
        {
            debug("cfgmgr32 shim:   -> %ls%ls\n", parent_id, L"");
            return CR_SUCCESS;
        }
    }
    *parent = 0;
    return CR_NO_SUCH_DEVNODE;
}

CONFIGRET WINAPI CM_Get_Parent(PDEVINST parent, DEVINST child, ULONG flags)
{
    return CM_Get_Parent_Ex(parent, child, flags, NULL);
}

/* Replace WINEBUS\WINE_COMP_HID by the compatible IDs of a USB HID interface. */
static CONFIGRET fix_compatible_ids(DEVINST node, CONFIGRET ret, PBYTE buffer, ULONG capacity, PULONG size)
{
    ULONG needed = sizeof(usb_hid_compatible_ids);
    if (ret != CR_SUCCESS && ret != CR_BUFFER_SMALL && ret != CR_NO_SUCH_VALUE) return ret;
    if (ret == CR_SUCCESS && buffer && multi_sz_mentions_usb((const WCHAR *)buffer)) return ret;
    if (!is_winebus_usb_node(node)) return ret;
    if (ret == CR_BUFFER_SMALL && *size >= needed) return ret;   /* original is longer; keep asking */
    if (!buffer || capacity < needed)
    {
        *size = needed;
        return CR_BUFFER_SMALL;
    }
    memcpy(buffer, usb_hid_compatible_ids, needed);
    *size = needed;
    debug("cfgmgr32 shim: compatible ids of a winebus USB node reported as USB HID%ls%ls\n", L"", L"");
    return CR_SUCCESS;
}

CONFIGRET WINAPI CM_Get_DevNode_Property_ExW(DEVINST node, const DEVPROPKEY *key, DEVPROPTYPE *type,
                                             PBYTE buffer, PULONG size, ULONG flags, HMACHINE machine)
{
    CONFIGRET ret;
    ULONG capacity = size ? *size : 0;
    if (!resolve()) return CR_FAILURE;
    ret = p_CM_Get_DevNode_Property_ExW(node, key, type, buffer, size, flags, machine);
    if (key && size && type && IsEqualDevPropKey(*key, DEVPKEY_Device_CompatibleIds))
    {
        ret = fix_compatible_ids(node, ret, buffer, capacity, size);
        if (ret == CR_SUCCESS || ret == CR_BUFFER_SMALL) *type = DEVPROP_TYPE_STRING_LIST;
    }
    return ret;
}

CONFIGRET WINAPI CM_Get_DevNode_PropertyW(DEVINST node, const DEVPROPKEY *key, DEVPROPTYPE *type,
                                          PBYTE buffer, PULONG size, ULONG flags)
{
    return CM_Get_DevNode_Property_ExW(node, key, type, buffer, size, flags, NULL);
}

CONFIGRET WINAPI CM_Get_DevNode_Registry_Property_ExW(DEVINST node, ULONG property, PULONG type,
                                                      PVOID buffer, PULONG size, ULONG flags, HMACHINE machine)
{
    CONFIGRET ret;
    ULONG capacity = size ? *size : 0;
    if (!resolve()) return CR_FAILURE;
    ret = p_CM_Get_DevNode_Registry_Property_ExW(node, property, type, buffer, size, flags, machine);
    if (property == CM_DRP_COMPATIBLEIDS && size)
    {
        ret = fix_compatible_ids(node, ret, buffer, capacity, size);
        if (type && (ret == CR_SUCCESS || ret == CR_BUFFER_SMALL)) *type = REG_MULTI_SZ;
    }
    return ret;
}

CONFIGRET WINAPI CM_Get_DevNode_Registry_PropertyW(DEVINST node, ULONG property, PULONG type,
                                                   PVOID buffer, PULONG size, ULONG flags)
{
    return CM_Get_DevNode_Registry_Property_ExW(node, property, type, buffer, size, flags, NULL);
}

BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason, LPVOID reserved)
{
    if (reason == DLL_PROCESS_ATTACH) DisableThreadLibraryCalls(instance);
    return TRUE;
}
