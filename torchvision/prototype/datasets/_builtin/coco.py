import functools
import io
import pathlib
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

import torch
from torchdata.datapipes.iter import (
    IterDataPipe,
    Mapper,
    # Shuffler,
    Filter,
    Demultiplexer,
    ZipArchiveReader,
    Grouper,
    IterKeyZipper,
    JsonParser,
    UnBatcher,
    Concater,
)
from torchvision.prototype.datasets.utils import (
    Dataset,
    DatasetConfig,
    DatasetInfo,
    HttpResource,
    OnlineResource,
    DatasetType,
)
from torchvision.prototype.datasets.utils._internal import (
    MappingIterator,
    INFINITE_BUFFER_SIZE,
    BUILTIN_DIR,
    getitem,
    path_accessor,
    path_comparator,
)


class Coco(Dataset):
    def _decode_instances_ann(self, ann: Dict[str, Any]) -> Dict[str, Any]:
        area = ann["area"]
        iscrowd = bool(ann["iscrowd"])
        bbox = torch.tensor(ann["bbox"])
        category = self.categories.index(ann["category_id"])
        id = ann["id"]
        return dict(
            area=area,
            iscrowd=iscrowd,
            bbox=bbox,
            category=category,
            id=id,
        )

    def _decode_captions_ann(self, ann: Dict[str, Any]) -> Dict[str, Any]:
        ann = ann.copy()
        ann.pop("image_id")
        return ann

    _ANN_TYPES, _ANN_TYPE_DEFAULTS, _ANN_DECODERS = zip(
        *(
            ("instances", True, _decode_instances_ann),
            ("captions", False, _decode_captions_ann),
        )
    )
    _ANN_TYPE_OPTIONS = dict(zip(_ANN_TYPES, [(default, not default) for default in _ANN_TYPE_DEFAULTS]))
    _ANN_DECODER_MAP = dict(zip(_ANN_TYPES, _ANN_DECODERS))

    def _make_info(self) -> DatasetInfo:
        return DatasetInfo(
            "coco",
            type=DatasetType.IMAGE,
            categories=BUILTIN_DIR / "coco.categories",
            homepage="https://cocodataset.org/",
            valid_options=dict(
                self._ANN_TYPE_OPTIONS,
                split=("train", "val"),
                year=("2017", "2014"),
            ),
        )

    _IMAGE_URL_BASE = "http://images.cocodataset.org/zips"

    _IMAGES_CHECKSUMS = {
        ("2014", "train"): "ede4087e640bddba550e090eae701092534b554b42b05ac33f0300b984b31775",
        ("2014", "val"): "fe9be816052049c34717e077d9e34aa60814a55679f804cd043e3cbee3b9fde0",
        ("2017", "train"): "69a8bb58ea5f8f99d24875f21416de2e9ded3178e903f1f7603e283b9e06d929",
        ("2017", "val"): "4f7e2ccb2866ec5041993c9cf2a952bbed69647b115d0f74da7ce8f4bef82f05",
    }

    _META_URL_BASE = "http://images.cocodataset.org/annotations"

    _META_CHECKSUMS = {
        "2014": "031296bbc80c45a1d1f76bf9a90ead27e94e99ec629208449507a4917a3bf009",
        "2017": "113a836d90195ee1f884e704da6304dfaaecff1f023f49b6ca93c4aaae470268",
    }

    def resources(self, config: DatasetConfig) -> List[OnlineResource]:
        images = HttpResource(
            f"{self._IMAGE_URL_BASE}/{config.split}{config.year}.zip",
            sha256=self._IMAGES_CHECKSUMS[(config.year, config.split)],
        )
        meta = HttpResource(
            f"{self._META_URL_BASE}/annotations_trainval{config.year}.zip",
            sha256=self._META_CHECKSUMS[config.year],
        )
        return [images, meta]

    _META_FILE_PATTERN = re.compile(fr"(?P<ann_type>({'|'.join(_ANN_TYPES)}))_(?P<split>[a-zA-Z]+)(?P<year>\d+)[.]json")

    def _classifiy_meta_files(
        self, data: Tuple[str, Any], *, split: str, year: str, ann_type_idcs: Dict[str, int]
    ) -> Optional[int]:
        match = self._META_FILE_PATTERN.match(pathlib.Path(data[0]).name)
        if not match or match["split"] != split or match["year"] != year:
            return None

        return ann_type_idcs.get(match["ann_type"])

    def _classify_meta(self, data: Tuple[str, Any]) -> Optional[int]:
        key, _ = data
        if key == "images":
            return 0
        elif key == "annotations":
            return 1
        else:
            return None

    def _make_partial_anns_dp(self, meta_dp: IterDataPipe[Tuple[str, io.IOBase]]) -> IterDataPipe:
        meta_dp = JsonParser(meta_dp)
        meta_dp = Mapper(meta_dp, getitem(1))
        meta_dp = MappingIterator(meta_dp)
        images_meta_dp, anns_meta_dp = Demultiplexer(
            meta_dp,
            2,
            self._classify_meta,
            drop_none=True,
            buffer_size=INFINITE_BUFFER_SIZE,
        )

        images_meta_dp = Mapper(images_meta_dp, getitem(1))
        images_meta_dp = UnBatcher(images_meta_dp)

        anns_meta_dp = Mapper(anns_meta_dp, getitem(1))
        anns_meta_dp = UnBatcher(anns_meta_dp)

        partial_anns_dp = Grouper(anns_meta_dp, group_key_fn=getitem("image_id"), buffer_size=INFINITE_BUFFER_SIZE)

        return IterKeyZipper(
            partial_anns_dp,
            images_meta_dp,
            key_fn=getitem(0, "image_id"),
            ref_key_fn=getitem("id"),
            buffer_size=INFINITE_BUFFER_SIZE,
        )

    def _precollate_anns(
        self, data: List[Tuple[List[Dict[str, Any]], Dict[str, Any]]], *, types: List[str]
    ) -> Tuple[str, Dict[str, Dict[str, Any]]]:
        ann_data, (image_data, *_) = zip(*data)
        return image_data["file_name"], dict(zip(types, ann_data))

    def _make_anns_dp(self, meta_dp, *, config):
        ann_types = [type for type in self._ANN_TYPES if config[type]]

        meta_dp = ZipArchiveReader(meta_dp)

        partial_anns_dps = Demultiplexer(
            meta_dp,
            len(ann_types),
            functools.partial(
                self._classifiy_meta_files,
                split=config.split,
                year=config.year,
                type_idcs=dict(zip(ann_types, range(len(ann_types)))),
            ),
            drop_none=True,
            buffer_size=INFINITE_BUFFER_SIZE,
        )
        partial_anns_dps = [self._make_partial_anns_dp(dp) for dp in partial_anns_dps]

        anns_dp = Concater(*partial_anns_dps)
        anns_dp = Grouper(anns_dp, group_key_fn=getitem(1, "id"), buffer_size=INFINITE_BUFFER_SIZE)
        # Can this be empty ? if yes, drop
        return Mapper(anns_dp, self._precollate_anns, fn_kwargs=dict(types=ann_types))

    def _collate_and_decode_sample(
        self,
        data: Tuple[Tuple[str, Dict[str, Dict[str, Any]]], Tuple[str, io.IOBase]],
        *,
        decoder: Optional[Callable[[io.IOBase], torch.Tensor]],
    ) -> Dict[str, Any]:
        ann_data, image_data = data
        _, anns = ann_data
        path, buffer = image_data

        anns = {type: self._ANN_DECODER_MAP[type](self, ann) for type, ann in anns.items()}

        image = decoder(buffer) if decoder else buffer

        return dict(anns, path=path, image=image)

    def _make_datapipe(
        self,
        resource_dps: List[IterDataPipe],
        *,
        config: DatasetConfig,
        decoder: Optional[Callable[[io.IOBase], torch.Tensor]],
    ) -> IterDataPipe[Dict[str, Any]]:
        images_dp, meta_dp = resource_dps

        anns_dp = self._make_anns_dp(meta_dp, config=config)
        # anns_dp = Shuffler(anns_dp, buffer_size=INFINITE_BUFFER_SIZE)

        dp = IterKeyZipper(
            anns_dp,
            images_dp,
            key_fn=getitem(0),
            ref_key_fn=path_accessor("name"),
            buffer_size=INFINITE_BUFFER_SIZE,
        )
        return Mapper(dp, self._collate_and_decode_sample, fn_kwargs=dict(decoder=decoder))

    def _generate_categories(self, root: pathlib.Path) -> List[str]:
        config = self.default_config
        resources = self.resources(config)

        dp = resources[1].to_datapipe(pathlib.Path(root) / self.name)
        dp = ZipArchiveReader(dp)
        dp = Filter(dp, path_comparator("name", f"instances_{config.split}{config.year}.json"))
        dp = JsonParser(dp)

        _, meta = next(iter(dp))
        categories_and_ids = [(info["name"], info["id"]) for info in meta["categories"]]
        categories, _ = zip(*sorted(categories_and_ids, key=lambda category_and_id: category_and_id[1]))

        return categories
