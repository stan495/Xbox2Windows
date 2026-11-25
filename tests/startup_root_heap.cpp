#include <windows.h>
#include <d3d12.h>
#include <d3d12sdklayers.h>
#include <wrl/client.h>

#include <atomic>
#include <cstdio>
#include <set>

#include "startup_stream_setup.h"

// X2W_TEST_DEBUG_LAYER=1 with X2W_TEST_ROOT_CBV (startup_root_cbv.cpp) and optional X2W_TEST_ROOT_FLAGS: every
// debug layer message of severity error or worse is counted through a message callback.
//
//   root_heap_control   with root signature flags, the title root signature's blob is set on a plain desktop list
//                       with no descriptor heaps. That must produce at least one message (the fail-first control);
//                       the IDs it produced are the ones counted afterwards. Without one, the run is a skip (77).
//   root_heap_report    after the stand-in's list ran: how many messages had a control ID (ID 1419 is always
//                       counted), and how many other errors there were.
namespace {
using Microsoft::WRL::ComPtr;
std::atomic<int> errors{0};
std::set<int> control_ids;
std::set<int> seen_ids;
SRWLOCK ids_lock = SRWLOCK_INIT;
int counted = 0, other = 0;
ComPtr<ID3D12InfoQueue1> queue1;

void WINAPI on_message(D3D12_MESSAGE_CATEGORY, D3D12_MESSAGE_SEVERITY severity, D3D12_MESSAGE_ID id, LPCSTR text, void*)
{
    if (severity > D3D12_MESSAGE_SEVERITY_ERROR) return;
    AcquireSRWLockExclusive(&ids_lock);
    seen_ids.insert(static_cast<int>(id));
    if (static_cast<int>(id) == 1419 || control_ids.count(static_cast<int>(id))) ++counted;
    else ++other;
    ReleaseSRWLockExclusive(&ids_lock);
    if (++errors <= 12) std::printf("debug layer id %d: %.300s\n", static_cast<int>(id), text);
}
}  // namespace

int root_heap_control(ID3D12Device* desktop)
{
    if (!GetEnvironmentVariableW(L"X2W_TEST_DEBUG_LAYER", nullptr, 0)) return 0;
    DWORD cookie = 0;
    if (FAILED(desktop->QueryInterface(IID_PPV_ARGS(&queue1))) ||
        FAILED(queue1->RegisterMessageCallback(on_message, D3D12_MESSAGE_CALLBACK_FLAG_NONE, nullptr, &cookie))) {
        std::printf("no debug layer message callback: skipped\n");
        return 77;
    }
    if (!GetEnvironmentVariableW(L"X2W_TEST_ROOT_FLAGS", nullptr, 0)) {
        std::printf("control: none (no root signature flags)\n");
        return 0;
    }
    const std::vector<char>& blob = stream_root_blob();
    D3D12_COMMAND_QUEUE_DESC queue_desc{};
    ComPtr<ID3D12CommandQueue> queue;
    ComPtr<ID3D12CommandAllocator> allocator;
    ComPtr<ID3D12GraphicsCommandList> list;
    ComPtr<ID3D12RootSignature> root;
    ComPtr<ID3D12Fence> fence;
    if (blob.empty() || FAILED(desktop->CreateRootSignature(0, blob.data(), blob.size(), IID_PPV_ARGS(&root)))) {
        std::printf("the desktop device rejects the root signature: skipped\n");
        return 77;
    }
    if (FAILED(desktop->CreateCommandQueue(&queue_desc, IID_PPV_ARGS(&queue))) ||
        FAILED(desktop->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, IID_PPV_ARGS(&allocator))) ||
        FAILED(desktop->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, allocator.Get(), nullptr, IID_PPV_ARGS(&list))) ||
        FAILED(desktop->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(&fence)))) return 50;
    list->SetGraphicsRootSignature(root.Get());
    list->Close();
    ID3D12CommandList* lists[] = {list.Get()};
    queue->ExecuteCommandLists(1, lists);
    queue->Signal(fence.Get(), 1);
    const ULONGLONG deadline = GetTickCount64() + 10000;
    while (fence->GetCompletedValue() < 1 && GetTickCount64() < deadline) Sleep(1);
    if (fence->GetCompletedValue() < 1) return 51;
    AcquireSRWLockExclusive(&ids_lock);
    control_ids = seen_ids;
    seen_ids.clear();
    const int control = counted + other;
    counted = other = 0;
    ReleaseSRWLockExclusive(&ids_lock);
    std::printf("control: %d messages, ids", control);
    for (int id : control_ids) std::printf(" %d", id);
    std::printf("\n");
    if (!control) {
        std::printf("the debug layer reports nothing for the control: skipped\n");
        return 77;
    }
    return 0;
}

int root_heap_report()
{
    if (!queue1) return 0;
    AcquireSRWLockExclusive(&ids_lock);
    const int stand_in = counted, rest = other;
    ReleaseSRWLockExclusive(&ids_lock);
    std::printf("stand-in list: %d messages with a control id or id 1419, %d other errors\n", stand_in, rest);
    return 0;
}
