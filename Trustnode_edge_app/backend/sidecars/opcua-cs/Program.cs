// TrustNode OPC UA sidecar (operator 2026-06-17).
//
// Standalone OPC UA server using the OPC Foundation .NET Standard
// reference stack. Spawned as a child process by Python; the Python
// manager passes runtime config via CLI args and pushes tag updates
// via a tiny HTTP control channel bound only to 127.0.0.1.
//
// Architecture:
//   * One Opc.Ua.StandardServer hosting a NodeManager that materialises
//     <gateway>/<device>/<tag> folder tree dynamically as tags arrive
//     via the control channel.
//   * HTTP control channel (HttpListener) on 127.0.0.1:<controlPort>
//     accepting:
//       POST /update     {gateway, device, tag, value, ts, quality}
//       POST /shutdown   {}
//   * No file-based config — everything is constructed in code so the
//     sidecar is a single self-contained exe with no companion files.
//
// CLI args:
//   --opc-port <int>      OPC UA endpoint port (default 4840)
//   --control-port <int>  loopback HTTP control port (default 14840)
//   --server-name <str>   advertised server name
//   --anonymous           anonymous-only auth
//   --username <str>      username when not anonymous
//   --password <str>      password when not anonymous

using System.Net;
using System.Text;
using System.Text.Json;
using Opc.Ua;
using Opc.Ua.Configuration;
using Opc.Ua.Server;

namespace TrustNode.OpcUa;

public static class Program
{
    public static async Task<int> Main(string[] args)
    {
        var opts = CliOptions.Parse(args);
        try
        {
            var config = BuildAppConfig(opts);
            var application = new ApplicationInstance
            {
                ApplicationName = opts.ServerName,
                ApplicationType = ApplicationType.Server,
                ApplicationConfiguration = config,
            };
            // Auto-accept any cert TrustNode produces — LAN-only scope,
            // operator can rotate the on-disk PKI via Settings.
            await application.CheckApplicationInstanceCertificates(true, 2048);

            var server = new TrustNodeServer();
            await application.Start(server);

            using var http = new HttpListener();
            http.Prefixes.Add($"http://127.0.0.1:{opts.ControlPort}/");
            http.Start();
            Console.Error.WriteLine($"[opcua] ready opc={opts.OpcPort} ctl={opts.ControlPort}");

            var shutdown = new TaskCompletionSource();
            _ = Task.Run(async () =>
            {
                while (http.IsListening)
                {
                    HttpListenerContext ctx;
                    try { ctx = await http.GetContextAsync(); }
                    catch { break; }
                    _ = Task.Run(() => HandleControl(ctx, server, shutdown));
                }
            });

            await shutdown.Task;
            try { http.Stop(); } catch { }
            try { server.Stop(); } catch { }
            return 0;
        }
        catch (ServiceResultException sre)
        {
            Console.Error.WriteLine($"[opcua] fatal ServiceResult: {sre.Result?.ToLongString() ?? sre.Message}");
            var inner = sre.InnerException;
            int depth = 0;
            while (inner != null && depth++ < 5)
            {
                Console.Error.WriteLine($"[opcua] inner[{depth}]: {inner.GetType().Name}: {inner.Message}");
                inner = inner.InnerException;
            }
            Console.Error.WriteLine($"[opcua] stack: {sre.StackTrace}");
            return 1;
        }
        catch (Exception ex)
        {
            var msg = new StringBuilder();
            msg.Append($"[opcua] fatal: {ex.GetType().Name}: {ex.Message}");
            var inner = ex.InnerException;
            int depth = 0;
            while (inner != null && depth++ < 5)
            {
                msg.Append($" | inner[{depth}]: {inner.GetType().Name}: {inner.Message}");
                inner = inner.InnerException;
            }
            msg.AppendLine();
            msg.Append("[opcua] stack: ");
            msg.Append(ex.StackTrace ?? "");
            Console.Error.WriteLine(msg.ToString());
            return 1;
        }
    }

    private static ApplicationConfiguration BuildAppConfig(CliOptions opts)
    {
        var baseDir = AppContext.BaseDirectory;
        var pkiRoot = Path.Combine(Path.GetTempPath(), "trustnode-opcua-pki");
        Directory.CreateDirectory(pkiRoot);

        var cfg = new ApplicationConfiguration
        {
            ApplicationName = opts.ServerName,
            ApplicationUri = $"urn:trustnode:edge:{Environment.MachineName}",
            ProductUri = "https://trustnode.io",
            ApplicationType = ApplicationType.Server,
            SecurityConfiguration = new SecurityConfiguration
            {
                ApplicationCertificate = new CertificateIdentifier
                {
                    StoreType = "Directory",
                    StorePath = Path.Combine(pkiRoot, "own"),
                    SubjectName = $"CN={opts.ServerName}, O=TrustNode, DC={Environment.MachineName}",
                },
                TrustedIssuerCertificates = new CertificateTrustList
                {
                    StoreType = "Directory",
                    StorePath = Path.Combine(pkiRoot, "issuer"),
                },
                TrustedPeerCertificates = new CertificateTrustList
                {
                    StoreType = "Directory",
                    StorePath = Path.Combine(pkiRoot, "trusted"),
                },
                RejectedCertificateStore = new CertificateStoreIdentifier
                {
                    StoreType = "Directory",
                    StorePath = Path.Combine(pkiRoot, "rejected"),
                },
                AutoAcceptUntrustedCertificates = true,
                AddAppCertToTrustedStore = true,
            },
            TransportConfigurations = new TransportConfigurationCollection(),
            TransportQuotas = new TransportQuotas { OperationTimeout = 15000 },
            ServerConfiguration = new ServerConfiguration
            {
                BaseAddresses = new StringCollection { $"opc.tcp://0.0.0.0:{opts.OpcPort}/trustnode/edge" },
                SecurityPolicies = new ServerSecurityPolicyCollection
                {
                    new ServerSecurityPolicy
                    {
                        SecurityMode = MessageSecurityMode.None,
                        SecurityPolicyUri = SecurityPolicies.None,
                    },
                },
                UserTokenPolicies = new UserTokenPolicyCollection
                {
                    opts.Anonymous
                        ? new UserTokenPolicy(UserTokenType.Anonymous)
                        : new UserTokenPolicy(UserTokenType.UserName),
                },
                MaxSessionCount = 100,
                MinSessionTimeout = 10000,
                MaxSessionTimeout = 3600000,
                MaxBrowseContinuationPoints = 10,
                MaxQueryContinuationPoints = 10,
                MaxHistoryContinuationPoints = 100,
                MaxRequestAge = 600000,
                MinPublishingInterval = 100,
                MaxPublishingInterval = 3600000,
                PublishingResolution = 50,
                DiagnosticsEnabled = false,
                ShutdownDelay = 1,
            },
            TraceConfiguration = new TraceConfiguration { OutputFilePath = Path.Combine(pkiRoot, "trace.log"), DeleteOnLoad = true, TraceMasks = 0 },
            CertificateValidator = new CertificateValidator(),
        };
        return cfg;
    }

    private static void HandleControl(HttpListenerContext ctx, TrustNodeServer server, TaskCompletionSource shutdown)
    {
        try
        {
            var path = ctx.Request.Url?.AbsolutePath ?? "/";
            if (path == "/shutdown")
            {
                ctx.Response.StatusCode = 200;
                ctx.Response.Close();
                shutdown.TrySetResult();
                return;
            }
            if (path == "/update" && ctx.Request.HttpMethod == "POST")
            {
                using var sr = new StreamReader(ctx.Request.InputStream, Encoding.UTF8);
                var body = sr.ReadToEnd();
                using var doc = JsonDocument.Parse(body);
                var root = doc.RootElement;
                var gateway = root.GetProperty("gateway").GetString() ?? "";
                var device = root.GetProperty("device").GetString() ?? "";
                var tag = root.GetProperty("tag").GetString() ?? "";
                double value = root.TryGetProperty("value", out var v) && v.ValueKind == JsonValueKind.Number ? v.GetDouble() : 0.0;
                server.UpdateTag(gateway, device, tag, value);
                var resp = Encoding.UTF8.GetBytes("{\"ok\":true}");
                ctx.Response.ContentType = "application/json";
                ctx.Response.OutputStream.Write(resp);
                ctx.Response.Close();
                return;
            }
            ctx.Response.StatusCode = 404;
            ctx.Response.Close();
        }
        catch (Exception ex)
        {
            try
            {
                ctx.Response.StatusCode = 500;
                var msg = Encoding.UTF8.GetBytes($"{{\"ok\":false,\"err\":\"{ex.Message.Replace("\"","'")}\"}}");
                ctx.Response.OutputStream.Write(msg);
                ctx.Response.Close();
            } catch { }
        }
    }
}

internal sealed class CliOptions
{
    public int OpcPort { get; init; } = 4840;
    public int ControlPort { get; init; } = 14840;
    public string ServerName { get; init; } = "TrustNode Edge OPC UA";
    public bool Anonymous { get; init; } = true;
    public string Username { get; init; } = "";
    public string Password { get; init; } = "";

    public static CliOptions Parse(string[] argv)
    {
        int opc = 4840, ctl = 14840;
        string name = "TrustNode Edge OPC UA";
        bool anon = true;
        string user = "", pass = "";
        for (int i = 0; i < argv.Length; i++)
        {
            switch (argv[i])
            {
                case "--opc-port": opc = int.Parse(argv[++i]); break;
                case "--control-port": ctl = int.Parse(argv[++i]); break;
                case "--server-name": name = argv[++i]; break;
                case "--anonymous": anon = true; break;
                case "--no-anonymous": anon = false; break;
                case "--username": user = argv[++i]; break;
                case "--password": pass = argv[++i]; break;
            }
        }
        return new CliOptions { OpcPort = opc, ControlPort = ctl, ServerName = name, Anonymous = anon, Username = user, Password = pass };
    }
}
