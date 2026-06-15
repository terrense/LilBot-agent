"""
排序算法性能对比基准测试
========================
实现 5 种主流排序算法并对比时间/空间复杂度
输出结果到 experiment/result.txt
"""

import random
import sys
import time

sys.setrecursionlimit(20000)


# ==================== 排序算法实现 ====================

def quick_sort(arr):
    """
    快速排序 (Quick Sort)
    时间复杂度: O(n log n) 平均, O(n²) 最坏
    空间复杂度: O(log n) 递归栈 (平均)
    """
    if len(arr) <= 1:
        return arr
    pivot = arr[len(arr) // 2]
    left = [x for x in arr if x < pivot]
    middle = [x for x in arr if x == pivot]
    right = [x for x in arr if x > pivot]
    return quick_sort(left) + middle + quick_sort(right)


def merge_sort(arr):
    """
    归并排序 (Merge Sort)
    时间复杂度: O(n log n) 稳定
    空间复杂度: O(n) 额外数组
    """
    if len(arr) <= 1:
        return arr
    mid = len(arr) // 2
    left = merge_sort(arr[:mid])
    right = merge_sort(arr[mid:])

    merged = []
    i = j = 0
    while i < len(left) and j < len(right):
        if left[i] <= right[j]:
            merged.append(left[i])
            i += 1
        else:
            merged.append(right[j])
            j += 1
    merged.extend(left[i:])
    merged.extend(right[j:])
    return merged


def heap_sort(arr):
    """
    堆排序 (Heap Sort)
    时间复杂度: O(n log n) 稳定
    空间复杂度: O(1) 原地排序
    """
    a = arr[:]  # 复制一份避免修改原数组
    n = len(a)

    def heapify(n, i):
        largest = i
        l = 2 * i + 1
        r = 2 * i + 2
        if l < n and a[l] > a[largest]:
            largest = l
        if r < n and a[r] > a[largest]:
            largest = r
        if largest != i:
            a[i], a[largest] = a[largest], a[i]
            heapify(n, largest)

    # 建堆
    for i in range(n // 2 - 1, -1, -1):
        heapify(n, i)
    # 逐个提取元素
    for i in range(n - 1, 0, -1):
        a[0], a[i] = a[i], a[0]
        heapify(i, 0)
    return a


def insertion_sort(arr):
    """
    插入排序 (Insertion Sort)
    时间复杂度: O(n²) 平均, O(n) 最好
    空间复杂度: O(1) 原地排序
    """
    a = arr[:]
    for i in range(1, len(a)):
        key = a[i]
        j = i - 1
        while j >= 0 and a[j] > key:
            a[j + 1] = a[j]
            j -= 1
        a[j + 1] = key
    return a


def bubble_sort(arr):
    """
    冒泡排序 (Bubble Sort)
    时间复杂度: O(n²) 平均, O(n) 最好 (优化后)
    空间复杂度: O(1) 原地排序
    """
    a = arr[:]
    n = len(a)
    for i in range(n):
        swapped = False
        for j in range(0, n - i - 1):
            if a[j] > a[j + 1]:
                a[j], a[j + 1] = a[j + 1], a[j]
                swapped = True
        if not swapped:
            break
    return a


# ==================== 正确性验证 ====================

def verify_sorts():
    """用小规模随机数据验证所有排序函数的正确性"""
    print("=" * 60)
    print("正在验证排序算法正确性...")
    for _ in range(10):
        lst = [random.randint(-1000, 1000) for _ in range(random.randint(1, 20))]
        expected = sorted(lst)
        assert quick_sort(lst) == expected, "quick_sort 验证失败"
        assert merge_sort(lst) == expected, "merge_sort 验证失败"
        assert heap_sort(lst) == expected, "heap_sort 验证失败"
        assert insertion_sort(lst) == expected, "insertion_sort 验证失败"
        assert bubble_sort(lst) == expected, "bubble_sort 验证失败"
    print("✅ 所有排序算法正确性验证通过！")
    print()


# ==================== 基准测试 ====================

def benchmark():
    """对不同规模的随机数据进行排序性能测试"""
    sizes = [100, 1000, 5000, 10000]
    algorithms = [
        ("快速排序", quick_sort, "O(n log n) 平均"),
        ("归并排序", merge_sort, "O(n log n)"),
        ("堆排序", heap_sort, "O(n log n)"),
        ("插入排序", insertion_sort, "O(n²)"),
        ("冒泡排序", bubble_sort, "O(n²)"),
    ]
    space_complexities = {
        "快速排序": "O(log n)",
        "归并排序": "O(n)",
        "堆排序": "O(1)",
        "插入排序": "O(1)",
        "冒泡排序": "O(1)",
    }
    repeats = 3

    print("开始基准测试...")
    print(f"数据规模: {sizes}")
    print(f"每种规模重复 {repeats} 次取平均")
    print()

    with open("experiment/result.txt", "w", encoding="utf-8") as f:
        # 写入表头
        f.write("=" * 100 + "\n")
        f.write("排序算法性能对比基准测试报告\n")
        f.write("=" * 100 + "\n\n")
        f.write(f"{'算法名称':<12} {'数据规模':<10} {'第1次(s)':<12} {'第2次(s)':<12} {'第3次(s)':<12} {'平均时间(s)':<14} {'空间复杂度':<12}\n")
        f.write("-" * 100 + "\n")

        for algo_name, algo_func, time_note in algorithms:
            print(f"  ▶ 测试 {algo_name}...")
            for size in sizes:
                times = []
                for rep in range(repeats):
                    data = [random.randint(0, 100000) for _ in range(size)]
                    start = time.perf_counter()
                    algo_func(data)
                    end = time.perf_counter()
                    times.append(end - start)

                avg = sum(times) / repeats
                sp = space_complexities[algo_name]
                f.write(f"{algo_name:<12} {size:<10} {times[0]:<12.6f} {times[1]:<12.6f} {times[2]:<12.6f} {avg:<14.6f} {sp:<12}\n")

            f.write("-" * 100 + "\n")

        # 补充空间复杂度汇总表
        f.write("\n\n")
        f.write("=" * 60 + "\n")
        f.write("空间复杂度汇总\n")
        f.write("=" * 60 + "\n")
        f.write(f"{'算法':<12} {'时间复杂度':<20} {'空间复杂度':<12}\n")
        f.write("-" * 60 + "\n")
        for algo_name, _, time_note in algorithms:
            f.write(f"{algo_name:<12} {time_note:<20} {space_complexities[algo_name]:<12}\n")

    print()
    print("✅ 基准测试完成，结果已写入 experiment/result.txt")


# ==================== 主入口 ====================

if __name__ == "__main__":
    print()
    print("=== 排序算法性能对比基准测试 ===")
    print()
    verify_sorts()
    benchmark()
    print()
    print("=" * 60)
    print("测试完成！请查看 experiment/result.txt")
    print("=" * 60)
