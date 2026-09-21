using System;
using System.IO;
using System.Resources;

internal static class PackResources
{
    private static int Main(string[] args)
    {
        if (args.Length < 2)
        {
            Console.Error.WriteLine("用法：PackResources 输出文件 输入资源 [输入资源…]");
            return 1;
        }
        try
        {
            using (var writer = new ResourceWriter(args[0]))
            {
                for (int index = 1; index < args.Length; index++)
                {
                    string path = args[index];
                    writer.AddResource("fonts/" + Path.GetFileName(path).ToLowerInvariant(),
                        new MemoryStream(File.ReadAllBytes(path)), true);
                }
                writer.Generate();
            }
            return 0;
        }
        catch (Exception error)
        {
            Console.Error.WriteLine("资源打包失败（" + args[0] + "）：" + error.Message);
            return 1;
        }
    }
}
