from pathlib import Path
import argparse
import sys

from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont


ROOT = Path(__file__).resolve().parents[2]
FONTS = ROOT / "dashboard" / "src" / "assets" / "fonts"
INSTANCES = (
    ("google-sans-flex-latin.woff2", "GoogleSansFlex-SemiBold.ttf", "Google Sans Flex", "SemiBold", 650, 32),
    ("roboto-latin.woff2", "Roboto-Regular.ttf", "Roboto", "Regular", 400, None),
    ("roboto-latin.woff2", "Roboto-SemiBold.ttf", "Roboto", "SemiBold", 600, None),
)


def prepare(output):
    output.mkdir(parents=True, exist_ok=True)
    for source, target, family, style, weight, optical_size in INSTANCES:
        with TTFont(FONTS / source, recalcTimestamp=False) as variable:
            axes = {axis.axisTag: axis.defaultValue for axis in variable["fvar"].axes}
            axes["wght"] = weight
            if optical_size is not None and "opsz" in axes:
                axes["opsz"] = optical_size
            font = instantiateVariableFont(variable, axes)
            font.flavor = None
            names = {
                1: family, 2: style, 4: family + " " + style,
                6: family.replace(" ", "") + "-" + style,
                16: family, 17: style,
            }
            unique_name = font["name"].getDebugName(3)
            if not unique_name:
                raise ValueError(f"字体缺少唯一标识 nameID 3：{source}")
            names[3] = unique_name.rsplit(";", 1)[0] + ";" + names[6]
            for name_id, value in names.items():
                font["name"].removeNames(nameID=name_id)
                font["name"].setName(value, name_id, 3, 1, 0x409)
            font["OS/2"].usWeightClass = weight
            if "STAT" in font:
                del font["STAT"]
            font.save(output / target)
            font.close()
            print("字体已生成：" + str(output / target))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="生成 NotmyFault 安装器使用的静态字体。")
    parser.add_argument("output", type=Path, help="字体输出目录")
    args = parser.parse_args()
    try:
        prepare(args.output.resolve())
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, "字体生成失败：" + str(error) + "\n")
