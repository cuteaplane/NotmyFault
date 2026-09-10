from pathlib import Path
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
            names[3] = font["name"].getDebugName(3).rsplit(";", 1)[0] + ";" + names[6]
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
    prepare(Path(sys.argv[1]).resolve())
