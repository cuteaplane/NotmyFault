using System;
using System.IO;
using System.Resources;

internal static class PackResources
{
    private static int Main(string[] args)
    {
        if (args.Length < 2) return 1;
        using (var writer = new ResourceWriter(args[0]))
        {
            for (int index = 1; index < args.Length; index++)
            {
                string path = args[index];
                writer.AddResource("fonts/" + Path.GetFileName(path).ToLowerInvariant(),
                    File.OpenRead(path), true);
            }
            writer.Generate();
        }
        return 0;
    }
}
