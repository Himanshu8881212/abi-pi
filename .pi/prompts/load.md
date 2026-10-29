---
description: Build everything from a folder of files (csv, parquet, json, xlsx)
argument-hint: "<folder> <project> [workspace]"
---
Load every data file in $1 into the ABI project $2 (workspace ${3:-main}) with abi_load. Then: make the model Ready
(fix what readiness lists), give the starter measures clear names and descriptions and add the obvious ones the data
needs, improve the Overview report so it shows the main measures by date and by the main categories, abi_sync, and tell
me in a few lines what you built and the paths of the project view and the report view.
