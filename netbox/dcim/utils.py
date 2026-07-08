from collections import defaultdict

from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.db import router, transaction
from django.utils.translation import gettext as _

from dcim.constants import MODULE_TOKEN


def get_module_bay_positions(module_bay):
    """
    Given a module bay, traverse up the module hierarchy and return
    a list of bay position strings from root to leaf, resolving any
    {module} tokens in each position using the parent position
    (position inheritance).
    """
    positions = []
    while module_bay:
        pos = module_bay.position or ''
        if positions and MODULE_TOKEN in pos:
            pos = pos.replace(MODULE_TOKEN, positions[-1])
        positions.append(pos)
        if module_bay.module:
            module_bay = module_bay.module.module_bay
        else:
            module_bay = None
    positions.reverse()
    return positions


def resolve_module_placeholder(value, positions):
    """
    Resolve {module} placeholder tokens in a string using the given
    list of module bay positions (ordered root to leaf).

    A single {module} token resolves to the leaf (immediate parent) bay's position.
    Multiple tokens must match the tree depth and resolve level-by-level.

    Returns the resolved string.
    Raises ValueError if token count is greater than 1 and doesn't match tree depth.
    """
    if MODULE_TOKEN not in value:
        return value

    token_count = value.count(MODULE_TOKEN)
    if token_count == 1:
        return value.replace(MODULE_TOKEN, positions[-1])
    if token_count == len(positions):
        for pos in positions:
            value = value.replace(MODULE_TOKEN, pos, 1)
        return value
    raise ValueError(
        _("Cannot install module with placeholder values in a module bay tree "
          "{level} levels deep but {tokens} placeholders given.").format(
            level=len(positions), tokens=token_count
        )
    )


def compile_path_node(ct_id, object_id):
    return f'{ct_id}:{object_id}'


def decompile_path_node(repr):
    ct_id, object_id = repr.split(':')
    return int(ct_id), int(object_id)


def object_to_path_node(obj):
    """
    Return a representation of an object suitable for inclusion in a CablePath path. Node representation is in the
    form <ContentType ID>:<Object ID>.
    """
    ct = ContentType.objects.get_for_model(obj)
    return compile_path_node(ct.pk, obj.pk)


def path_node_to_object(repr):
    """
    Given the string representation of a path node, return the corresponding instance. If the object no longer
    exists, return None.
    """
    ct_id, object_id = decompile_path_node(repr)
    ct = ContentType.objects.get_for_id(ct_id)
    return ct.model_class().objects.filter(pk=object_id).first()


def create_cablepaths(objects):
    """
    Create CablePaths for all paths originating from the specified set of nodes.

    :param objects: Iterable of cabled objects (e.g. Interfaces)
    """
    from dcim.models import CablePath, Interface

    # Expand any channelized interface into its channel subinterfaces. A channelized parent originates no path of its
    # own; instead, each channel subinterface traces independently from the single connector position it occupies.
    # Plain (non-channelized) origins pass through unchanged, keeping this expansion re-entrant so that
    # rebuild_paths() -> create_cablepaths(cp.origins) does not re-expand the channel subinterfaces it already holds.
    expanded = []
    for obj in objects:
        if isinstance(obj, Interface) and obj.channels:
            expanded.extend(obj.child_interfaces.filter(channel_id__isnull=False, cable__isnull=False))
        else:
            expanded.append(obj)

    # Arrange objects by cable connector. All objects with a null connector are grouped together. Channel
    # subinterfaces must each originate their own path, as sharing a connector would otherwise collapse a group of
    # siblings into a single malformed path.
    origins = defaultdict(list)
    for obj in expanded:
        if isinstance(obj, Interface) and obj.channel_id:
            if cp := CablePath.from_origin([obj]):
                cp.save()
        else:
            origins[obj.cable_connector].append(obj)

    for connector, objects in origins.items():
        if cp := CablePath.from_origin(objects):
            cp.save()


def rebuild_paths(terminations):
    """
    Rebuild all CablePaths which traverse the specified nodes.
    """
    from dcim.models import CablePath

    for obj in terminations:
        cable_paths = CablePath.objects.filter(_nodes__contains=obj)

        with transaction.atomic(using=router.db_for_write(CablePath)):
            for cp in cable_paths:
                cp.delete()
                create_cablepaths(cp.origins)


def rebuild_cable_paths(cable):
    """
    Delete and rebuild every CablePath traversing the given Cable, tracing freshly from the Cable's current
    terminations in both directions. Used when the channelization of a terminated interface changes (e.g. a channel
    subinterface is added, moved, or removed) without the Cable itself being modified.
    """
    from dcim.choices import CableEndChoices
    from dcim.models import CablePath, CableTermination, PathEndpoint

    with transaction.atomic(using=router.db_for_write(CablePath)):
        # Delete existing paths individually so each clears its `_path` back-reference on the originating endpoints.
        for cp in CablePath.objects.filter(_nodes__contains=cable):
            cp.delete()

        a_terminations, b_terminations = [], []
        for ct in CableTermination.objects.filter(cable=cable):
            if ct.cable_end == CableEndChoices.SIDE_A:
                a_terminations.append(ct.termination)
            else:
                b_terminations.append(ct.termination)

        for nodes in (a_terminations, b_terminations):
            if not nodes:
                continue
            if isinstance(nodes[0], PathEndpoint):
                create_cablepaths(nodes)
            else:
                rebuild_paths(nodes)


def update_interface_parents(device, interface_templates, module=None):
    """
    Used for device and module instantiation. Iterates all InterfaceTemplates with a parent assigned and applies it to
    the actual interfaces. Must run after all interfaces have been instantiated (so that every parent interface exists)
    and before update_interface_bridges() (so that channel subinterfaces validate against a populated parent).
    """
    Interface = apps.get_model('dcim', 'Interface')

    for interface_template in interface_templates.exclude(parent=None):
        interface = Interface.objects.get(device=device, name=interface_template.resolve_name(module=module))
        interface.parent = Interface.objects.get(
            device=device,
            name=interface_template.parent.resolve_name(module=module)
        )
        interface.full_clean()
        interface.save()


def update_interface_bridges(device, interface_templates, module=None):
    """
    Used for device and module instantiation. Iterates all InterfaceTemplates with a bridge assigned
    and applies it to the actual interfaces.
    """
    Interface = apps.get_model('dcim', 'Interface')

    for interface_template in interface_templates.exclude(bridge=None):
        interface = Interface.objects.get(device=device, name=interface_template.resolve_name(module=module))

        if interface_template.bridge:
            interface.bridge = Interface.objects.get(
                device=device,
                name=interface_template.bridge.resolve_name(module=module)
            )
            interface.full_clean()
            interface.save()


def create_port_mappings(device, device_or_module_type, module=None):
    """
    Replicate all front/rear port mappings from a DeviceType or ModuleType to the given device.
    """
    from dcim.models import FrontPort, PortMapping, RearPort

    templates = device_or_module_type.port_mappings.prefetch_related('front_port', 'rear_port')

    # Cache front & rear ports for efficient lookups by name
    front_ports = {
        fp.name: fp for fp in FrontPort.objects.filter(device=device)
    }
    rear_ports = {
        rp.name: rp for rp in RearPort.objects.filter(device=device)
    }

    # Replicate PortMappings
    mappings = []
    for template in templates:
        front_port = front_ports.get(template.front_port.resolve_name(module=module))
        rear_port = rear_ports.get(template.rear_port.resolve_name(module=module))
        mappings.append(
            PortMapping(
                device_id=front_port.device_id,
                front_port=front_port,
                front_port_position=template.front_port_position,
                rear_port=rear_port,
                rear_port_position=template.rear_port_position,
            )
        )
    PortMapping.objects.bulk_create(mappings)
