/*
 * Test tool: walk every HID device interface up the device tree the way hidapi does on
 * Windows (interface -> instance ID -> devnode -> CM_Get_Parent ...) and print what each
 * step returns, including the compatible IDs hidapi uses to detect the bus type.
 */

#include <windows.h>
#include <initguid.h>
#include <cfgmgr32.h>
#include <devpkey.h>
#include <hidclass.h>
#include <setupapi.h>
#include <stdio.h>

#ifndef SPDRP_BASE_CONTAINERID
#define SPDRP_BASE_CONTAINERID 0x24
#endif

static void print_multi_sz(const WCHAR *s)
{
    for (; s && *s; s += wcslen(s) + 1) wprintf(L" [%ls]", s);
}

static void show_node(DEVINST node, int depth)
{
    WCHAR id[MAX_DEVICE_ID_LEN];
    BYTE buf[4096];
    ULONG size = sizeof(buf);
    DEVPROPTYPE type;
    CONFIGRET ret;
    DEVINST parent;

    ret = CM_Get_Device_IDW(node, id, ARRAYSIZE(id), 0);
    wprintf(L"%*ls node %lu id %ls (ret %lu)\n", depth * 2, L"", node, ret ? L"?" : id, ret);
    ret = CM_Get_DevNode_PropertyW(node, &DEVPKEY_Device_CompatibleIds, &type, buf, &size, 0);
    wprintf(L"%*ls   compatible ids (ret %lu):", depth * 2, L"", ret);
    if (!ret) print_multi_sz((WCHAR *)buf);
    wprintf(L"\n");
    if (depth >= 3) return;
    ret = CM_Get_Parent(&parent, node, 0);
    wprintf(L"%*ls   CM_Get_Parent -> ret %lu, node %lu\n", depth * 2, L"", ret, ret ? 0 : parent);
    if (!ret && parent) show_node(parent, depth + 1);
}

int wmain(int argc, WCHAR **argv)
{
    ULONG len = 0;
    WCHAR *list, *iface;
    const WCHAR *filter = argc > 1 ? argv[1] : L"VID_054C";

    if (argc > 2 && !wcscmp(argv[1], L"regprops"))
    {
        /* The SetupAPI registry properties games read for every device (0,1,7,8,9,12,36). */
        static const DWORD props[] = { SPDRP_DEVICEDESC, SPDRP_HARDWAREID, SPDRP_CLASS, SPDRP_CLASSGUID,
                                       SPDRP_DRIVER, SPDRP_FRIENDLYNAME, SPDRP_BASE_CONTAINERID };
        HDEVINFO set = SetupDiGetClassDevsW(NULL, NULL, NULL, DIGCF_ALLCLASSES);
        SP_DEVINFO_DATA data = { sizeof(data) };
        DWORD i, p;
        for (i = 0; SetupDiEnumDeviceInfo(set, i, &data); i++)
        {
            WCHAR id[MAX_DEVICE_ID_LEN];
            SetupDiGetDeviceInstanceIdW(set, &data, id, ARRAYSIZE(id), NULL);
            if (!wcsstr(id, argv[2])) continue;
            wprintf(L"%ls\n", id);
            for (p = 0; p < ARRAYSIZE(props); p++)
            {
                BYTE buf[1024] = {0};
                DWORD type = 0;
                if (SetupDiGetDeviceRegistryPropertyW(set, &data, props[p], &type, buf, sizeof(buf), NULL))
                    wprintf(L"  prop %lu (type %lu): %ls\n", props[p], type, (WCHAR *)buf);
                else
                    wprintf(L"  prop %lu: error %lu\n", props[p], GetLastError());
            }
        }
        return 0;
    }

    if (argc > 2 && !wcscmp(argv[1], L"locate"))
    {
        int i;
        for (i = 2; i < argc; i++)
        {
            DEVINST node = 0;
            CONFIGRET normal = CM_Locate_DevNodeW(&node, argv[i], CM_LOCATE_DEVNODE_NORMAL);
            CONFIGRET phantom = CM_Locate_DevNodeW(&node, argv[i], CM_LOCATE_DEVNODE_PHANTOM);
            wprintf(L"%ls: normal %lu, phantom %lu (node %lu)\n", argv[i], normal, phantom, node);
        }
        return 0;
    }

    if (CM_Get_Device_Interface_List_SizeW(&len, (GUID *)&GUID_DEVINTERFACE_HID, NULL, 0) || !len) return 1;
    list = calloc(len, sizeof(WCHAR));
    if (CM_Get_Device_Interface_ListW((GUID *)&GUID_DEVINTERFACE_HID, NULL, list, len, 0)) return 1;
    for (iface = list; *iface; iface += wcslen(iface) + 1)
    {
        WCHAR instance[MAX_DEVICE_ID_LEN];
        ULONG size = sizeof(instance);
        DEVPROPTYPE type;
        DEVINST node;
        CONFIGRET ret;

        if (!wcsstr(iface, filter)) continue;
        wprintf(L"interface %ls\n", iface);
        ret = CM_Get_Device_Interface_PropertyW(iface, &DEVPKEY_Device_InstanceId, &type, (BYTE *)instance, &size, 0);
        if (ret) { wprintf(L"  instance id: ret %lu\n", ret); continue; }
        ret = CM_Locate_DevNodeW(&node, instance, CM_LOCATE_DEVNODE_NORMAL);
        wprintf(L"  locate %ls -> ret %lu\n", instance, ret);
        if (!ret) show_node(node, 1);
    }
    return 0;
}
