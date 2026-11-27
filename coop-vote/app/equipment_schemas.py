"""机具预约/出借接口的请求模型。

输出直接返回 equipment 服务组装的 dict；这里只负责入参校验和常见字段别名，
方便本地脚本与不同调用习惯。
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _first(data: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in data and data[name] is not None:
            return data[name]
    return None


class EquipmentCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    asset_no: str = Field(min_length=1, max_length=64)
    name: str = Field(default="", max_length=128)
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _aliases(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = dict(data)
        if out.get("asset_no") is None:
            value = _first(data, "asset_no", "asset_code", "equipment_no", "code",
                           "serial_number", "serial_no", "number")
            if value is not None:
                out["asset_no"] = str(value).strip()
        if out.get("asset_no") is not None and not isinstance(out["asset_no"], str):
            out["asset_no"] = str(out["asset_no"])
        if not out.get("tags"):
            value = _first(data, "tags", "capability_tags", "capabilities",
                           "labels", "tag_list", "tag")
            if isinstance(value, str):
                value = [value]
            if value is not None:
                out["tags"] = value
        if not out.get("name"):
            value = _first(data, "name", "title", "model")
            if value is not None:
                out["name"] = value
        return out


class ReservationItemIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    label: str = Field(default="", max_length=128)
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _aliases(cls, data: Any) -> Any:
        if isinstance(data, str):
            return {"tags": [data]}
        if isinstance(data, (list, tuple, set, frozenset)):
            return {"tags": list(data)}
        if not isinstance(data, dict):
            return data
        out = dict(data)
        if not out.get("tags"):
            tags = _first(data, "tags", "required_tags", "capability_tags",
                          "capabilities", "labels", "tag")
            if isinstance(tags, str):
                tags = [tags]
            if tags is not None:
                out["tags"] = tags
        if not out.get("label"):
            label = _first(data, "label", "name", "title", "item_name")
            if label is not None:
                out["label"] = label
        return out


class ReservationCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    starts_at: Any
    ends_at: Any
    items: list[ReservationItemIn] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _aliases(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = dict(data)
        if out.get("starts_at") is None:
            value = _first(data, "starts_at", "start_at", "start_time", "start",
                           "borrow_start", "from")
            if value is not None:
                out["starts_at"] = value
        if out.get("ends_at") is None:
            value = _first(data, "ends_at", "end_at", "end_time", "end",
                           "borrow_end", "to", "until")
            if value is not None:
                out["ends_at"] = value
        if not out.get("items"):
            value = _first(data, "items", "required_items", "checklist",
                           "requirements", "list", "tag_sets",
                           "required_tag_sets")
            if value is not None:
                out["items"] = value
        if not out.get("items"):
            raw_tags = _first(data, "required_tags", "tags")
            if isinstance(raw_tags, list) and raw_tags:
                if all(isinstance(tag_set, (list, tuple, set, frozenset))
                       for tag_set in raw_tags):
                    out["items"] = [{"tags": list(tag_set)} for tag_set in raw_tags]
                else:
                    # 一维字符串数组表示“一个清单项需要全部标签”。
                    out["items"] = [{"tags": raw_tags}]
        return out


class MaintenanceCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    equipment_id: Optional[int] = None
    asset_no: Optional[str] = None
    starts_at: Any
    ends_at: Any
    reason: str = ""

    @model_validator(mode="before")
    @classmethod
    def _aliases(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = dict(data)
        if out.get("starts_at") is None:
            value = _first(data, "starts_at", "start_at", "start_time", "start", "from")
            if value is not None:
                out["starts_at"] = value
        if out.get("ends_at") is None:
            value = _first(data, "ends_at", "end_at", "end_time", "end", "to", "until")
            if value is not None:
                out["ends_at"] = value
        if out.get("asset_no") is None:
            value = _first(data, "asset_no", "asset_code", "equipment_no", "code",
                           "serial_number")
            if value is not None:
                out["asset_no"] = value
        if out.get("asset_no") is not None and not isinstance(out["asset_no"], str):
            out["asset_no"] = str(out["asset_no"])
        return out


class HandoverIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    equipment_ids: Optional[list[int]] = None
    asset_nos: Optional[list[str]] = None

    @model_validator(mode="before")
    @classmethod
    def _aliases(cls, data: Any) -> Any:
        if data is None:
            return {}
        if isinstance(data, list):
            # 允许直接提交 ["ASSET-1", "ASSET-2"]，按字符串/整数自动归类。
            ints = [x for x in data if isinstance(x, int) and not isinstance(x, bool)]
            strs = [x for x in data if isinstance(x, str)]
            return {"equipment_ids": ints or None, "asset_nos": strs or None}
        if not isinstance(data, dict):
            return {}
        out = dict(data)
        if out.get("equipment_ids") is None:
            value = _first(data, "equipment_ids", "ids", "equipment_id", "id")
            if isinstance(value, int):
                out["equipment_ids"] = [value]
            elif isinstance(value, list):
                out["equipment_ids"] = value
        if out.get("asset_nos") is None:
            value = _first(
                data,
                "asset_nos",
                "asset_no",
                "asset_numbers",
                "asset_number",
                "asset_codes",
                "asset_code",
                "serial_numbers",
                "serial_number",
                "serial_nos",
                "numbers",
            )
            if isinstance(value, str):
                out["asset_nos"] = [value]
            elif isinstance(value, list):
                out["asset_nos"] = value
        assignments = data.get("assignments")
        if isinstance(assignments, list) and assignments:
            ids: list[int] = []
            nos: list[str] = []
            for assignment in assignments:
                if not isinstance(assignment, dict):
                    continue
                eid = assignment.get("equipment_id") or assignment.get("id")
                no = (assignment.get("asset_no") or assignment.get("asset_code")
                      or assignment.get("serial_number"))
                if eid is not None:
                    ids.append(int(eid))
                elif no is not None:
                    nos.append(str(no))
            if ids and not out.get("equipment_ids"):
                out["equipment_ids"] = ids
            if nos and not out.get("asset_nos"):
                out["asset_nos"] = nos
        if out.get("asset_nos") is not None:
            out["asset_nos"] = [str(value) for value in out["asset_nos"] if value is not None]
        return out


class ReturnIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    loan_id: Optional[int] = None
    equipment_id: Optional[int] = None
    asset_no: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _aliases(cls, data: Any) -> Any:
        if data is None:
            return {}
        if isinstance(data, (str, int)):
            if isinstance(data, int):
                return {"loan_id": data}
            return {"asset_no": data}
        if not isinstance(data, dict):
            return {}
        out = dict(data)
        if out.get("loan_id") is None:
            value = _first(data, "loan_id", "id")
            if value is not None:
                out["loan_id"] = value
        if out.get("asset_no") is None:
            value = _first(data, "asset_no", "asset_code", "serial_number", "number")
            if value is not None:
                out["asset_no"] = value
        if out.get("asset_no") is not None and not isinstance(out["asset_no"], str):
            out["asset_no"] = str(out["asset_no"])
        return out
