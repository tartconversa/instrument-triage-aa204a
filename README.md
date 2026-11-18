# 共同工况变化下的仪表复核优先级

此目录保存用于产品开发的合成业务资料、字段契约、资料包自检工具，以及在此之上实现的数据应用。
资料说明见 `docs/资料口径.md`，文件目录及完整性摘要见 `data/catalog.json`，分析方法与业务口径见
`docs/分析说明.md`。应用只给分析复核建议，不控制生产设备，也不进行业务批准。

## 环境与资料检查

使用 Python 3.11.2，依赖在 `requirements.txt` 中锁定。可在虚拟环境安装：

    python3 -m pip install -r requirements.txt
    python3 -m sourcepack

资料检查逐文件回报行数、键唯一性、完整性与关系引用是否符合资料目录；资料行中的拒绝、停试、缺测和不确定范围是业务记录，不是读入错误。

## 分析应用

    python3 -m steamcheck --summary
    python3 -m steamcheck --summary --as-of 2025-02-13T08:00:00+08:00
    python3 -m steamcheck --json --write-state out.json
    python3 -m steamcheck.serve --port 8765

`--summary` 输出证据单元、候选解释、单耗影响与复核排序；`--as-of` 指定"当时可知"时点，
只使用该时点 `known_at` 之前可知的观测与记录。`serve` 启动本地只读网页视图（默认 127.0.0.1:8765）。

应用回答三件事：先查什么、为什么值得查、查完以后哪些能源判断才有依据。它按运行档建立基线并
扣除共同负荷，因此负荷切换不会被当成多只表同时故障；同一变送器只生成一个证据单元，一次校验
可同时澄清多个测点。详见 `docs/分析说明.md`。

## 测试与语法构建

    python3 -m unittest discover -s tests -v
    python3 -m compileall -q sourcepack steamcheck tests

测试覆盖：资料可读取性与检查器诊断；负荷切换与共享变送器的检测行为；候选解释保留；
`known_at` 时间线（事后确认不得评价更早告警）；现场结果对指标与排序的更新；窗口中已占用
名额不释放；预算不足时保留未决结论；以及网页接口的基本可用性。

## 目录

    data/        合成资料
    docs/        资料口径与分析说明
    sourcepack/  资料容器检查器
    steamcheck/  分析应用（sources / anomaly / decisions / inspection / engine / cli / serve / web）
    tests/       资料检查与分析行为测试
