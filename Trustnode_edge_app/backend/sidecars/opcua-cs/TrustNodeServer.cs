// TrustNode OPC UA server — dynamic node manager that materialises
// gateway/device/tag folders on the fly so the Python side can push
// tag values without static configuration.

using Opc.Ua;
using Opc.Ua.Server;
using System.Collections.Concurrent;

namespace TrustNode.OpcUa;

public sealed class TrustNodeServer : StandardServer
{
    private TrustNodeNodeManager? _nodeManager;

    protected override MasterNodeManager CreateMasterNodeManager(IServerInternal server, ApplicationConfiguration configuration)
    {
        _nodeManager = new TrustNodeNodeManager(server, configuration);
        return new MasterNodeManager(server, configuration, null, new INodeManager[] { _nodeManager });
    }

    public void UpdateTag(string gateway, string device, string tag, double value)
    {
        _nodeManager?.UpdateTag(gateway, device, tag, value);
    }
}

public sealed class TrustNodeNodeManager : CustomNodeManager2
{
    private const string Namespace = "urn:trustnode:edge";
    private FolderState? _root;
    private readonly ConcurrentDictionary<string, BaseDataVariableState> _tags = new();
    private readonly ConcurrentDictionary<string, FolderState> _folders = new();
    private readonly object _addLock = new();

    public TrustNodeNodeManager(IServerInternal server, ApplicationConfiguration configuration)
        : base(server, configuration, Namespace) { }

    protected override NodeStateCollection LoadPredefinedNodes(ISystemContext context)
    {
        return new NodeStateCollection();
    }

    public override void CreateAddressSpace(IDictionary<NodeId, IList<IReference>> externalReferences)
    {
        lock (Lock)
        {
            if (!externalReferences.TryGetValue(ObjectIds.ObjectsFolder, out var references))
            {
                externalReferences[ObjectIds.ObjectsFolder] = references = new List<IReference>();
            }
            _root = new FolderState(null)
            {
                NodeId = new NodeId("TrustNode", NamespaceIndex),
                BrowseName = new QualifiedName("TrustNode", NamespaceIndex),
                DisplayName = new LocalizedText("TrustNode"),
                TypeDefinitionId = ObjectTypeIds.FolderType,
                EventNotifier = EventNotifiers.None,
            };
            _root.AddReference(ReferenceTypeIds.Organizes, true, ObjectIds.ObjectsFolder);
            references.Add(new NodeStateReference(ReferenceTypeIds.Organizes, false, _root.NodeId));
            AddPredefinedNode(SystemContext, _root);
        }
    }

    public void UpdateTag(string gateway, string device, string tag, double value)
    {
        if (string.IsNullOrEmpty(gateway) || string.IsNullOrEmpty(tag))
        {
            return;
        }
        device = string.IsNullOrEmpty(device) ? "device" : device;
        var key = $"{gateway}::{device}::{tag}";
        if (!_tags.TryGetValue(key, out var variable))
        {
            lock (_addLock)
            {
                if (!_tags.TryGetValue(key, out variable))
                {
                    variable = EnsureTagNode(gateway, device, tag);
                    _tags[key] = variable;
                }
            }
        }
        variable.Value = value;
        variable.Timestamp = DateTime.UtcNow;
        variable.StatusCode = StatusCodes.Good;
        variable.ClearChangeMasks(SystemContext, false);
    }

    private FolderState EnsureFolder(NodeState parent, string name, string id)
    {
        var key = $"folder::{id}";
        if (_folders.TryGetValue(key, out var existing)) return existing;
        var folder = new FolderState(parent)
        {
            NodeId = new NodeId(id, NamespaceIndex),
            BrowseName = new QualifiedName(name, NamespaceIndex),
            DisplayName = new LocalizedText(name),
            TypeDefinitionId = ObjectTypeIds.FolderType,
            EventNotifier = EventNotifiers.None,
        };
        parent.AddChild(folder);
        AddPredefinedNode(SystemContext, folder);
        _folders[key] = folder;
        return folder;
    }

    private BaseDataVariableState EnsureTagNode(string gateway, string device, string tag)
    {
        if (_root == null) throw new InvalidOperationException("address space not initialized");
        var gwFolder = EnsureFolder(_root, gateway, $"GW::{gateway}");
        var devFolder = EnsureFolder(gwFolder, device, $"GW::{gateway}::DEV::{device}");
        var variable = new BaseDataVariableState(devFolder)
        {
            NodeId = new NodeId($"GW::{gateway}::DEV::{device}::TAG::{tag}", NamespaceIndex),
            BrowseName = new QualifiedName(tag, NamespaceIndex),
            DisplayName = new LocalizedText(tag),
            TypeDefinitionId = VariableTypeIds.BaseDataVariableType,
            ReferenceTypeId = ReferenceTypeIds.Organizes,
            DataType = DataTypeIds.Double,
            ValueRank = ValueRanks.Scalar,
            AccessLevel = AccessLevels.CurrentRead,
            UserAccessLevel = AccessLevels.CurrentRead,
            Historizing = false,
            Value = 0.0,
            StatusCode = StatusCodes.Good,
            Timestamp = DateTime.UtcNow,
        };
        devFolder.AddChild(variable);
        AddPredefinedNode(SystemContext, variable);
        return variable;
    }
}
