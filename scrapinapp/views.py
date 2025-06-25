import asyncio, requests
from django.http import JsonResponse, Http404
from django.views.decorators.http import require_http_methods
from pathlib import Path
from django.conf import settings
from .utils import capture_sections_for_all_links, fetch_policies_parallel, ingest_policies_from_eramba
from .models import Certification, Clause, Policy, Control
from django.db import transaction
import json
from playwright.sync_api import sync_playwright
import uuid 
from django.db import IntegrityError
from django.utils.text import slugify
from playwright.async_api import async_playwright
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
from django.views.decorators.csrf import csrf_exempt

logger = logging.getLogger(__name__)

def get_certifications(request):
    if request.method != "GET":
        return JsonResponse({"status": "error", "message": "Only GET method is allowed."}, status=405)

    try:
        results = asyncio.run(capture_sections_for_all_links())
        return JsonResponse({
            "status": "success",
            "message": f"Captured {len(results)} section JSONs.",
            "files": [res["url"] for res in results]
        })
    except Exception as e:
        return JsonResponse({"status": "error", "message": str(e)}, status=500)


@require_http_methods(["GET"])
def populate_database(request):
    """Endpoint to populate DB from JSON files with M2M relationships"""
    BASE_DIR = Path(settings.BASE_DIR)
    OUTPUT_DIR = BASE_DIR / "sections_output"
    
    stats = {
        'files_processed': 0,
        'certifications': 0,
        'clauses': 0,
        'policies': 0,
        'controls': 0,
        'policy_clause_links': 0,
        'control_clause_links': 0,
        'errors': []
    }

    if not OUTPUT_DIR.exists():
        return JsonResponse({
            "status": "error",
            "message": f"Directory not found: {OUTPUT_DIR}"
        }, status=404)

    try:
        with transaction.atomic():
            for json_file in OUTPUT_DIR.glob("*.json"):
                try:
                    stats['files_processed'] += 1
                    with open(json_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        
                        # Process certification
                        cert_name = ' '.join([part.upper() for part in json_file.stem.split('_')])
                        cert, created = Certification.objects.get_or_create(name=cert_name)
                        if created:
                            stats['certifications'] += 1
                        
                        # Process clauses
                        for section in data:
                            clause, created = Clause.objects.get_or_create(
                                certification=cert,
                                reference_id=section.get("referenceId", ""),
                                defaults={
                                    'display_identifier': section.get("displayIdentifier", ""),
                                    'title': section.get("title", ""),
                                    'description': section.get("description", ""),
                                    'original_id': section.get("id", "")
                                }
                            )
                            if created:
                                stats['clauses'] += 1
                            
                            # Process policies with M2M
                            for policy_data in section.get("programPolicyMapping", []):
                                policy, created = Policy.objects.get_or_create(
                                    policy_id=policy_data.get("shortName", ""),
                                    defaults={
                                        'policy_reference': policy_data.get("id", ""),
                                        'policy_doc': policy_data.get("description", ""),
                                        'title': policy_data.get("title", ""),
                                        'policy_gathered_from':'TC'
                                    }
                                )
                                if created:
                                    stats['policies'] += 1
                                
                                # Add M2M relationship if not exists
                                if not clause.policies.filter(pk=policy.pk).exists():
                                    clause.policies.add(policy)
                                    stats['policy_clause_links'] += 1
                            
                            # Process controls with M2M
                            for subsection in section.get("subsections", []):
                                for control_data in subsection.get("programControlMapping", []):
                                    control, created = Control.objects.get_or_create(
                                        short_name=control_data.get("shortName", ""),
                                        defaults={
                                            'custom_short_name': control_data.get("customShortName", None),
                                            'name': control_data.get("name", ""),
                                            'description': control_data.get("description", ""),
                                            'original_id': control_data.get("id", "")
                                        }
                                    )
                                    if created:
                                        stats['controls'] += 1
                                    
                                    # Add M2M relationship if not exists
                                    if not clause.controls.filter(pk=control.pk).exists():
                                        clause.controls.add(control)
                                        stats['control_clause_links'] += 1
                
                except Exception as e:
                    stats['errors'].append({
                        'file': json_file.name,
                        'error': str(e)
                    })
        
        response_status = "success" if not stats['errors'] else "partial"
        status_code = 200 if not stats['errors'] else 207
        
        return JsonResponse({
            "status": response_status,
            "message": "Database population completed",
            "stats": stats
        }, status=status_code)
    
    except Exception as e:
        return JsonResponse({
            "status": "error",
            "message": f"Transaction failed: {str(e)}",
            "stats": stats
        }, status=500)

@require_http_methods(["GET"])
def get_population_status(request):
    """Check current database status with M2M counts"""
    from django.db.models import Count
    
    certifications = Certification.objects.count()
    clauses = Clause.objects.count()
    policies = Policy.objects.count()
    controls = Control.objects.count()
    
    # Get sample M2M relationship counts
    policy_with_clauses = Policy.objects.annotate(
        clause_count=Count('clauses')
    ).order_by('-clause_count').first()
    
    control_with_clauses = Control.objects.annotate(
        clause_count=Count('clauses')
    ).order_by('-clause_count').first()
    
    return JsonResponse({
        "status": "success",
        "data": {
            "certifications": certifications,
            "clauses": clauses,
            "policies": policies,
            "controls": controls,
            "sample_relationships": {
                "most_linked_policy": {
                    "id": policy_with_clauses.policy_id if policy_with_clauses else None,
                    "clause_count": policy_with_clauses.clause_count if policy_with_clauses else 0
                },
                "most_linked_control": {
                    "id": control_with_clauses.short_name if control_with_clauses else None,
                    "clause_count": control_with_clauses.clause_count if control_with_clauses else 0
                }
            }
        }
    })

OUTPUT_DIR = Path("policies_output")
OUTPUT_DIR.mkdir(exist_ok=True)


async def capture_policies_data():
    found = False
    collected_data = None

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        async def handle_response(response):
            nonlocal found, collected_data
            try:
                if "policies" in response.url and response.request.resource_type == "xhr":
                    found = True
                    print(f"✅ Found 'policies' API: {response.url}")
                    json_data = await response.json()

                    # Save JSON to file
                    output_path = OUTPUT_DIR / "trustcloud_policies.json"
                    with open(output_path, "w", encoding="utf-8") as f:
                        json.dump(json_data, f, ensure_ascii=False, indent=2)

                    control_ids_mapping_with_policies = []
                    for i in json_data:
                        policy_id = i.get("id")
                        control_ids = i.get("relatedControlIds")
                        security_group = i.get("securityGroup")
                        control_ids_mapping_with_policies.append({
                            "policy": policy_id,
                            "control_ids": control_ids,
                            "security_group":security_group
                        })

                    collected_data = control_ids_mapping_with_policies  # ✅ Set nonlocal
                    print(f"📁 Saved JSON to {output_path}")
            except Exception as e:
                print(f"⚠️ Error: {e}")

        page.on("response", handle_response)

        try:
            await page.goto("https://trust.trustcloud.ai/policies", timeout=30000)
            await page.wait_for_timeout(8000)  # Wait for XHR to complete
        except Exception as e:
            print(f"❌ Failed to load page: {e}")
        finally:
            await browser.close()

        if not found:
            print("❌ 'policies' API not found.")

    return collected_data


@csrf_exempt
def map_controls_with_policy(request):
    try:
        data = asyncio.run(capture_policies_data())
        if not data:
            return JsonResponse({"error": "No 'policies' API response captured"}, status=404)

        result = {
            "linked": [],
            "unmatched_policies": [],
            "unmatched_controls": []
        }

        for item in data:
            policy_ref = item.get("policy")
            control_ids = item.get("control_ids", [])

            try:
                policy = Policy.objects.get(policy_reference=policy_ref)
            except Policy.DoesNotExist:
                result["unmatched_policies"].append(policy_ref)
                continue

            matched_controls = []
            missing_controls = []

            for cid in control_ids:
                try:
                    ctrl = Control.objects.get(original_id=cid)
                    matched_controls.append(ctrl)
                except Control.DoesNotExist:
                    missing_controls.append(cid)

            # Update M2M relationship
            if matched_controls:
                policy.controls.set(matched_controls)
                policy.save()
                result["linked"].append({
                    "policy": policy_ref,
                    "matched_control_ids": [c.original_id for c in matched_controls]
                })

            if missing_controls:
                result["unmatched_controls"].append({
                    "policy": policy_ref,
                    "missing_control_ids": missing_controls
                })

        for i in data:
            policy = i.get("policy")
            security_group = i.get("security_group")
            if Policy.objects.filter(policy_reference=policy).exists():
                policyObj = Policy.objects.get(policy_reference=policy)
                policyObj.security_group = security_group
                policyObj.save()

        return JsonResponse(result, safe=False)

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)
    


from django.shortcuts import render, get_object_or_404
def certifications_view(request):
    certifications = Certification.objects.all().prefetch_related('clauses')
    return render(request, 'certifications.html', {
        'certifications': certifications
    })

def clause_detail_view(request, clause_id):
    clause = get_object_or_404(Clause, id=clause_id)
    return render(request, 'clause_detail.html', {
        'clause': clause
    })

from collections import defaultdict
from .models import Policy

def policies_view(request):
    policies = Policy.objects.prefetch_related('clauses', 'controls')
    group = request.GET.get("group", "ALL")

    all_groups = set()
    trustcloud_grouped = defaultdict(lambda: defaultdict(list))
    eramba_list = []

    for policy in policies:
        if policy.policy_gathered_from == 'TC':
            group_name = policy.security_group or "Uncategorized"
            trustcloud_grouped[group_name][policy.title or "Untitled"].append(policy)
            all_groups.add(group_name)
        elif policy.policy_gathered_from == 'ER':
            eramba_list.append(policy)

    # Choose what to render
    if group == "ALL":
        filtered_policies = policies
    elif group == "ER":
        filtered_policies = eramba_list
    elif group.startswith("TC__"):
        group_name = group.replace("TC__", "")
        filtered_policies = []
        if group_name in trustcloud_grouped:
            for title_group in trustcloud_grouped[group_name].values():
                filtered_policies.extend(title_group)
    else:
        filtered_policies = []

    context = {
        "filtered_policies": filtered_policies,
        "security_groups": sorted(all_groups),
        "selected_group": group,
    }
    return render(request, "policies.html", context)

def clause_detail_api(request, clause_id):
    try:
        clause = Clause.objects.select_related('certification').get(id=clause_id)
    except Clause.DoesNotExist:
        raise Http404

    return JsonResponse({
        "id": clause.id,
        "reference_id": clause.reference_id,
        "display_identifier": clause.display_identifier,
        "title": clause.title,
        "description": clause.description,
        "certification": clause.certification.name,
    })


def control_detail_api(request, control_id):
    try:
        control = Control.objects.get(id=control_id)
    except Control.DoesNotExist:
        raise Http404

    return JsonResponse({
        "id": control.id,
        "short_name": control.short_name,
        "name": control.name,
        "description": control.description,
        "original_id": control.original_id,
    })
    

def policy_detail_api(request, policy_id):
    try:
        policy = Policy.objects.select_related().prefetch_related('clauses', 'controls').get(pk=policy_id)
    except Policy.DoesNotExist:
        raise Http404("Policy not found")

    data = {
        "Policy ID": policy.policy_id,
        "Title": policy.title,
        "Reference": policy.policy_reference,
        "Version": policy.policy_version,
        "Description": policy.policy_doc,
        "Security Group": policy.security_group,
        "Clauses": [clause.display_identifier for clause in policy.clauses.all()],
        "Controls": [control.short_name for control in policy.controls.all()],
    }
    return JsonResponse(data)

def control_detail(request, id):
    control = get_object_or_404(Control, id=id)
    return render(request, 'control_detail.html', {'control': control})


@csrf_exempt
def pulling_policies_from_eramba(request):
    """API endpoint to fetch policies from eramba"""
    policies = fetch_policies_parallel()
    return JsonResponse({
        "message": f"Successfully pulled {len(policies)} policies",
        "policies": policies,
        "policies_count": len(policies)
    }, status=200)

@csrf_exempt
def pulling_eramba_frameworkds(request):
    url = "https://www.eramba.org/api/proxy?endpoint=compliance-package-regulators"
    new_certifications = []
    updated_certifications = []

    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
    except requests.exceptions.Timeout:
        return JsonResponse({"error": "Request to Eramba timed out."}, status=504)
    except requests.exceptions.RequestException as e:
        return JsonResponse({"error": f"Request failed: {str(e)}"}, status=500)

    try:
        content = response.json()
        data = content.get("data", [])
    except (ValueError, json.JSONDecodeError):
        return JsonResponse({"error": "Invalid JSON response from Eramba."}, status=502)

    for item in data:
        name = item.get("name")
        description = item.get("description")
        created_at = item.get("created")
        version = item.get("version")
        frameworkUrl = item.get("url")
        regulation_name = item.get("regulation_name")

        try:
            existing = Certification.objects.filter(name=name).first()
            if not existing:
                Certification.objects.create(
                    name=name,
                    slug=name.lower().replace(" ", "-"),
                    description=description,
                    url=frameworkUrl,
                    version=version,
                    regulation_name=regulation_name,
                    created_at=created_at,
                    updated_at=None
                )
                new_certifications.append(name)
            else:
                existing.version = version
                existing.description = description
                existing.url = frameworkUrl
                existing.regulation_name = regulation_name
                existing.save()
                updated_certifications.append(name)
        except Exception as db_error:
            print(f"Error processing certification '{name}': {str(db_error)}")

    return JsonResponse({
        "message": "Successfully pulled Eramba frameworks.",
        "new_certifications": new_certifications,
        "updated_certifications": updated_certifications
    })

def ingest_eramba_policies_view(request):
    if request.method != "GET":
        return JsonResponse({"success": False, "error": "Only GET method allowed"}, status=405)

    API_URL = "https://www.eramba.org/api/proxy?endpoint=security-policies"
    result = ingest_policies_from_eramba(API_URL)
    return JsonResponse(result)

@csrf_exempt
def policy_template_view(request, policy_id):
    try:
        policy = Policy.objects.get(pk=policy_id)
    except Policy.DoesNotExist:
        return JsonResponse({"error": "Policy not found"}, status=404)

    if request.method == "GET":
        if not policy.policy_template:
            return JsonResponse({"error": "Template not found."}, status=404)
        return JsonResponse({"template": policy.policy_template})

    elif request.method == "POST":
        try:
            data = json.loads(request.body)
            new_template = data.get("template", "")
            policy.policy_template = new_template
            policy.save()
            return JsonResponse({"success": True})
        except Exception as e:
            return JsonResponse({"error": str(e)}, status=400)
        
@csrf_exempt
def get_eramba_clauses(request):
    def fetch_clause(i):
        url = f"https://www.eramba.org/api/proxy?endpoint=compliance-package-regulators&action=show&id={i}"
        try:
            res = requests.get(url, timeout=5)
            data = res.json()
            if data != {'message': 'Internal server error'}:
                return data
        except Exception as e:
            logger.error(f"Failed to fetch clause for ID {i}: {str(e)}")
            return None
        return None

    # Fetch clauses from API
    clauses = []
    with ThreadPoolExecutor(max_workers=15) as executor:
        futures = {executor.submit(fetch_clause, i): i for i in range(100)}
        for future in as_completed(futures):
            result = future.result()
            if result:
                clauses.append(result)

    # Initialize counters for response
    total_certs_processed = 0
    total_clauses_created = 0
    total_clauses_updated = 0
    total_policies_mapped = 0
    total_controls_mapped = 0
    errors = []
    warnings = []

    # Process fetched clauses
    for clause_entry in clauses:
        cert_data = clause_entry.get('data', {})
        cert_name = cert_data.get('name')

        if not cert_name:
            warnings.append('Skipped clause entry with no name')
            logger.warning('Skipped clause entry with no name')
            continue

        with transaction.atomic():
            # Get or create Certification
            try:
                certification, cert_created = Certification.objects.get_or_create(
                    name=cert_name,
                    defaults={
                        'slug': slugify(cert_name),
                        'description': cert_data.get('description', ''),
                        'url': cert_data.get('url', ''),
                        'version': cert_data.get('version', ''),
                        'regulation_name': cert_data.get('regulation_name', '')
                    }
                )
                total_certs_processed += 1
                if cert_created:
                    warnings.append(f'Created new Certification: {cert_name}')
                    logger.info(f'Created new Certification: {cert_name}')
                else:
                    warnings.append(f'Using existing Certification: {cert_name}')
                    logger.info(f'Using existing Certification: {cert_name}')
            except Exception as e:
                errors.append(f'Error processing Certification "{cert_name}": {str(e)}')
                logger.error(f'Error processing Certification "{cert_name}": {str(e)}')
                continue

            # Process compliance packages
            compliance_packages = cert_data.get('compliance_packages', [])
            for package in compliance_packages:
                package_items = package.get('compliance_package_items', [])
                for item in package_items:
                    item_id = item.get('item_id')
                    item_name = item.get('name')
                    item_description = item.get('description', '')

                    if not item_id or not item_name:
                        warnings.append(f'Skipping item with missing item_id or name in package {package.get("name")}')
                        logger.warning(f'Skipping item with missing item_id or name in package {package.get("name")}')
                        continue

                    # Create or get Clause
                    try:
                        clause, created = Clause.objects.get_or_create(
                            certification=certification,
                            reference_id=item_id,
                            defaults={
                                'display_identifier': item_id,
                                'title': item_name,
                                'description': item_description,
                                'original_id': str(item.get('id')) if item.get('id') else None
                            }
                        )
                        if created:
                            total_clauses_created += 1
                            warnings.append(f'Created Clause: {item_id} - {item_name}')
                            logger.info(f'Created Clause: {item_id} - {item_name}')
                        else:
                            total_clauses_updated += 1
                            warnings.append(f'Updating existing Clause: {item_id} - {item_name}')
                            logger.info(f'Updating existing Clause: {item_id} - {item_name}')

                        # Debug security_services
                        compliance_management = item.get('compliance_management', {})
                        security_services = compliance_management.get('security_services', [])
                        logger.debug(f'Processing security_services for Clause {item_id}: {security_services}')

                        # Map Policies
                        security_policies = compliance_management.get('security_policies', [])
                        for policy_data in security_policies:
                            policy_index = policy_data.get('index')
                            if not policy_index:
                                continue
                            try:
                                policy = Policy.objects.get(title=policy_index)
                                clause.policies.add(policy)
                                total_policies_mapped += 1
                                warnings.append(f'Mapped Policy "{policy_index}" to Clause {item_id}')
                                logger.info(f'Mapped Policy "{policy_index}" to Clause {item_id}')
                            except Policy.DoesNotExist:
                                warnings.append(f'Policy "{policy_index}" not found for Clause {item_id}')
                                logger.warning(f'Policy "{policy_index}" not found for Clause {item_id}')

                        # Map Controls
                        for service_data in security_services:
                            service_name = service_data.get('name')
                            if not service_name:
                                warnings.append(f'Skipping empty service_name for Clause {item_id}')
                                logger.warning(f'Skipping empty service_name for Clause {item_id}')
                                continue
                            try:
                                control = Control.objects.get(name=service_name)
                                clause.controls.add(control)
                                total_controls_mapped += 1
                                warnings.append(f'Mapped Control "{service_name}" to Clause {item_id}')
                                logger.info(f'Mapped Control "{service_name}" to Clause {item_id}')
                            except Control.DoesNotExist:
                                warnings.append(f'Control "{service_name}" not found for Clause {item_id}')
                                logger.warning(f'Control "{service_name}" not found for Clause {item_id}')
                                # Optional: Create missing control
                                """
                                control, _ = Control.objects.get_or_create(
                                    name=service_name,
                                    defaults={
                                        'short_name': service_name[:50],
                                        'description': 'Auto-created control from Eramba API',
                                    }
                                )
                                clause.controls.add(control)
                                total_controls_mapped += 1
                                warnings.append(f'Created and mapped Control "{service_name}" to Clause {item_id}')
                                logger.info(f'Created and mapped Control "{service_name}" to Clause {item_id}')
                                """

                    except Exception as e:
                        errors.append(f'Error processing Clause {item_id} for Certification "{cert_name}": {str(e)}')
                        logger.error(f'Error processing Clause {item_id} for Certification "{cert_name}": {str(e)}')

    # Prepare response
    response_data = {
        'status': 'success' if not errors else 'partial_success',
        'clauses_fetched': len(clauses),
        'certifications_processed': total_certs_processed,
        'clauses_created': total_clauses_created,
        'clauses_updated': total_clauses_updated,
        'policies_mapped': total_policies_mapped,
        'controls_mapped': total_controls_mapped,
        'warnings': warnings,
        'errors': errors
    }
    logger.info(f'Response: {response_data}')
    return JsonResponse(response_data)

@csrf_exempt
def get_eramba_controls(request):
    """
    Fetches security controls and their associated policies from the Eramba API.
    It then synchronizes this data with the local Django database.

    For each control:
    1. Checks if the control already exists in the database by its 'short_name'.
    2. If not, creates a new Control record.
    3. Iterates through the policies associated with the control from the Eramba API.
    4. For each policy, checks if a Policy record with the same 'title' exists locally.
    5. If a policy does not exist, a new Policy record is created with unique 'policy_id'
       and 'policy_reference' generated from the policy's title and a UUID.
    6. Finally, establishes a many-to-many relationship between the Control and the Policy.
    """
    try:
        # Fetch data from the Eramba API
        req = requests.get("https://www.eramba.org/api/proxy?endpoint=security-services")
        req.raise_for_status()  # Raise HTTPError for bad responses (4xx or 5xx)
        data = (req.json()).get("data")

        if not data:
            return JsonResponse({"status": "error", "message": "No data received from Eramba API."}, status=400)

        controls_processed_count = 0
        policies_processed_count = 0
        policies_linked_count = 0

        # Iterate through each control item received from the Eramba API
        for item in data:
            short_name = item.get("id")
            name = item.get("name")
            
            # Concatenate description fields, providing empty strings for missing data
            description = (item.get("objective", "") + " " +
                           item.get("audit_metric_description", "") + " " +
                           item.get("audit_success_criteria", "")).strip() # Remove leading/trailing spaces

            # These fields are often not directly available or are null/blank from the external API for these specific models.
            custom_short_name = None # Eramba API might not provide this
            original_id = None       # Eramba API might not provide this
            created_at = item.get("created") # Use Eramba's created timestamp if available

            # Check if the control already exists in the database
            current_control = Control.objects.filter(short_name=short_name).first()

            if not current_control:
                # If the control does not exist, create a new one
                try:
                    current_control = Control.objects.create(
                        short_name=short_name,
                        custom_short_name=custom_short_name,
                        name=name,
                        description=description,
                        original_id=original_id,
                        created_at=created_at,
                        # updated_at will be set automatically by auto_now=True
                    )
                    controls_processed_count += 1
                except IntegrityError as e:
                    # Handle cases where a unique constraint might be violated (e.g., short_name)
                    print(f"Integrity error creating Control '{short_name}': {e}")
                    continue # Skip to the next control if creation fails
                except Exception as e:
                    print(f"Unexpected error creating Control '{short_name}': {e}")
                    continue # Skip to the next control

            # Process policies related to this control
            policies_data = item.get("security_policies", []) # Get the list of security policies
            for policy_entry in policies_data:
                policy_title = policy_entry.get("index") # Eramba uses 'index' for policy title
                if not policy_title:
                    print(f"Skipping policy with no title found for control '{short_name}'")
                    continue

                # Try to find an existing policy by its title
                policy_obj = Policy.objects.filter(title=policy_title).first()

                if not policy_obj:
                    # If the policy does not exist, create a new one
                    # Generate unique policy_id and policy_reference using slugify and UUID
                    base_slug = slugify(policy_title)
                    new_policy_id = f"{base_slug}-{uuid.uuid4().hex[:10]}"
                    new_policy_reference = f"{base_slug}-ref-{uuid.uuid4().hex[:10]}"

                    try:
                        policy_obj = Policy.objects.create(
                            policy_id=new_policy_id,
                            policy_reference=new_policy_reference,
                            title=policy_title,
                            policy_gathered_from='ER', # Mark as gathered from Eramba
                            security_group=None, # Assuming these are not directly in Eramba 'security_policies'
                            policy_doc=None,
                            policy_version=None,
                            policy_template=None,
                        )
                        policies_processed_count += 1
                    except IntegrityError as e:
                        print(f"Integrity error creating Policy '{policy_title}' (ID: {new_policy_id}): {e}")
                        continue # Skip to the next policy if creation fails
                    except Exception as e:
                        print(f"Unexpected error creating Policy '{policy_title}': {e}")
                        continue

                # Link the current control to the policy
                if current_control and policy_obj:
                    # Use .add() for ManyToManyField. It automatically handles duplicates.
                    # Check if already linked to avoid unnecessary database operations and increment counter correctly.
                    if not current_control.policies.filter(pk=policy_obj.pk).exists():
                        current_control.policies.add(policy_obj)
                        policies_linked_count += 1
                        # print(f"Linked control '{short_name}' to policy '{policy_title}'")
                    # else:
                        # print(f"Control '{short_name}' already linked to policy '{policy_title}'")

        return JsonResponse({
            "status": "success",
            "message": "Eramba controls and policies synchronized successfully.",
            "controls_processed": controls_processed_count,
            "policies_processed": policies_processed_count,
            "policies_linked_to_controls": policies_linked_count
        }, status=200)

    except requests.exceptions.RequestException as e:
        # Catch errors related to the HTTP request to the Eramba API
        return JsonResponse({"status": "error", "message": f"API request failed: {e}"}, status=500)
    except Exception as e:
        # Catch any other unexpected errors
        return JsonResponse({"status": "error", "message": f"An unexpected error occurred: {e}"}, status=500)