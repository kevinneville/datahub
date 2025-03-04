import json
import logging
from datetime import datetime
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

import dateutil.parser as dp
from pydantic import validator
from tableauserverclient import (
    PersonalAccessTokenAuth,
    Server,
    ServerResponseError,
    TableauAuth,
)

import datahub.emitter.mce_builder as builder
from datahub.configuration.common import ConfigModel, ConfigurationError
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.mcp_builder import (
    PlatformKey,
    add_entity_to_container,
    gen_containers,
)
from datahub.ingestion.api.common import PipelineContext
from datahub.ingestion.api.source import Source, SourceReport
from datahub.ingestion.api.workunit import MetadataWorkUnit
from datahub.ingestion.source.tableau_common import (
    FIELD_TYPE_MAPPING,
    MetadataQueryException,
    clean_query,
    custom_sql_graphql_query,
    get_field_value_in_sheet,
    get_tags_from_params,
    get_unique_custom_sql,
    make_description_from_params,
    make_table_urn,
    published_datasource_graphql_query,
    query_metadata,
    workbook_graphql_query,
)
from datahub.metadata.com.linkedin.pegasus2avro.common import (
    AuditStamp,
    ChangeAuditStamps,
)
from datahub.metadata.com.linkedin.pegasus2avro.dataset import (
    DatasetLineageTypeClass,
    UpstreamClass,
    UpstreamLineage,
)
from datahub.metadata.com.linkedin.pegasus2avro.metadata.snapshot import (
    ChartSnapshot,
    DashboardSnapshot,
    DatasetSnapshot,
)
from datahub.metadata.com.linkedin.pegasus2avro.mxe import MetadataChangeEvent
from datahub.metadata.com.linkedin.pegasus2avro.schema import (
    NullTypeClass,
    OtherSchema,
    SchemaField,
    SchemaFieldDataType,
    SchemaMetadata,
)
from datahub.metadata.schema_classes import (
    BrowsePathsClass,
    ChangeTypeClass,
    ChartInfoClass,
    DashboardInfoClass,
    DatasetPropertiesClass,
    OwnerClass,
    OwnershipClass,
    OwnershipTypeClass,
    SubTypesClass,
    ViewPropertiesClass,
)
from datahub.utilities import config_clean

logger: logging.Logger = logging.getLogger(__name__)

# Replace / with |
REPLACE_SLASH_CHAR = "|"


class TableauConfig(ConfigModel):
    connect_uri: str
    username: Optional[str] = None
    password: Optional[str] = None
    token_name: Optional[str] = None
    token_value: Optional[str] = None

    site: str = ""
    projects: Optional[List] = ["default"]
    default_schema_map: dict = {}
    ingest_tags: Optional[bool] = False
    ingest_owner: Optional[bool] = False

    workbooks_page_size: int = 10
    env: str = builder.DEFAULT_ENV

    @validator("connect_uri")
    def remove_trailing_slash(cls, v):
        return config_clean.remove_trailing_slashes(v)


class WorkbookKey(PlatformKey):
    workbook_id: str


class TableauSource(Source):
    config: TableauConfig
    report: SourceReport
    platform = "tableau"
    server: Server
    upstream_tables: Dict[str, Tuple[Any, str]] = {}

    def __hash__(self):
        return id(self)

    def __init__(self, ctx: PipelineContext, config: TableauConfig):
        super().__init__(ctx)

        self.config = config
        self.report = SourceReport()
        # This list keeps track of datasource being actively used by workbooks so that we only retrieve those
        # when emitting published data sources.
        self.datasource_ids_being_used: List[str] = []
        # This list keeps track of datasource being actively used by workbooks so that we only retrieve those
        # when emitting custom SQL data sources.
        self.custom_sql_ids_being_used: List[str] = []

        self._authenticate()

    def close(self) -> None:
        self.server.auth.sign_out()

    def _authenticate(self):
        # https://tableau.github.io/server-client-python/docs/api-ref#authentication
        authentication = None
        if self.config.username and self.config.password:
            authentication = TableauAuth(
                username=self.config.username,
                password=self.config.password,
                site_id=self.config.site,
            )
        elif self.config.token_name and self.config.token_value:
            authentication = PersonalAccessTokenAuth(
                self.config.token_name, self.config.token_value, self.config.site
            )
        else:
            raise ConfigurationError(
                "Tableau Source: Either username/password or token_name/token_value must be set"
            )

        try:
            self.server = Server(self.config.connect_uri, use_server_version=True)
            self.server.auth.sign_in(authentication)
        except ServerResponseError as e:
            self.report.report_failure(
                key="tableau-login",
                reason=f"Unable to Login with credentials provided" f"Reason: {str(e)}",
            )
        except Exception as e:
            self.report.report_failure(
                key="tableau-login", reason=f"Unable to Login" f"Reason: {str(e)}"
            )

    def get_connection_object(
        self,
        query: str,
        connection_type: str,
        query_filter: str,
        count: int = 0,
        current_count: int = 0,
    ) -> Tuple[dict, int, int]:
        query_data = query_metadata(
            self.server, query, connection_type, count, current_count, query_filter
        )

        if "errors" in query_data:
            self.report.report_warning(
                key="tableau-metadata",
                reason=f"Connection: {connection_type} Error: {query_data['errors']}",
            )

        connection_object = query_data.get("data", {}).get(connection_type, {})
        total_count = connection_object.get("totalCount", 0)
        has_next_page = connection_object.get("pageInfo", {}).get("hasNextPage", False)
        return connection_object, total_count, has_next_page

    def emit_workbooks(self, workbooks_page_size: int) -> Iterable[MetadataWorkUnit]:

        projects = (
            f"projectNameWithin: {json.dumps(self.config.projects)}"
            if self.config.projects
            else ""
        )

        workbook_connection, total_count, has_next_page = self.get_connection_object(
            workbook_graphql_query, "workbooksConnection", projects
        )

        current_count = 0
        while has_next_page:
            count = (
                workbooks_page_size
                if current_count + workbooks_page_size < total_count
                else total_count - current_count
            )
            (
                workbook_connection,
                total_count,
                has_next_page,
            ) = self.get_connection_object(
                workbook_graphql_query,
                "workbooksConnection",
                projects,
                count,
                current_count,
            )

            current_count += count

            for workbook in workbook_connection.get("nodes", []):
                yield from self.emit_workbook_as_container(workbook)
                yield from self.emit_sheets_as_charts(workbook)
                yield from self.emit_dashboards(workbook)
                yield from self.emit_embedded_datasource(workbook)
                yield from self.emit_upstream_tables()

    def _track_custom_sql_ids(self, field: dict) -> None:
        # Tableau shows custom sql datasource as a table in ColumnField.
        if field.get("__typename", "") == "ColumnField":
            for column in field.get("columns", []):
                table_id = column.get("table", {}).get("id")

                if (
                    table_id is not None
                    and table_id not in self.custom_sql_ids_being_used
                ):
                    self.custom_sql_ids_being_used.append(table_id)

    def _create_upstream_table_lineage(
        self, datasource: dict, project: str, is_custom_sql: bool = False
    ) -> List[UpstreamClass]:
        upstream_tables = []
        upstream_dbs = datasource.get("upstreamDatabases", [])
        upstream_db = upstream_dbs[0].get("name", "") if upstream_dbs else ""

        for table in datasource.get("upstreamTables", []):
            # skip upstream tables when there is no column info when retrieving embedded datasource
            # Schema details for these will be taken care in self.emit_custom_sql_ds()
            if not is_custom_sql and not table.get("columns"):
                continue

            schema = self._get_schema(table.get("schema", ""), upstream_db)
            table_urn = make_table_urn(
                self.config.env,
                upstream_db,
                table.get("connectionType", ""),
                schema,
                table.get("name", ""),
            )

            upstream_table = UpstreamClass(
                dataset=table_urn,
                type=DatasetLineageTypeClass.TRANSFORMED,
            )
            upstream_tables.append(upstream_table)
            table_path = f"{project.replace('/', REPLACE_SLASH_CHAR)}/{datasource.get('name', '')}/{table.get('name', '')}"
            self.upstream_tables[table_urn] = (
                table.get("columns", []),
                table_path,
            )
        return upstream_tables

    def emit_custom_sql_datasources(self) -> Iterable[MetadataWorkUnit]:
        count_on_query = len(self.custom_sql_ids_being_used)
        custom_sql_filter = "idWithin: {}".format(
            json.dumps(self.custom_sql_ids_being_used)
        )
        custom_sql_connection, total_count, has_next_page = self.get_connection_object(
            custom_sql_graphql_query, "customSQLTablesConnection", custom_sql_filter
        )

        current_count = 0
        while has_next_page:
            count = (
                count_on_query
                if current_count + count_on_query < total_count
                else total_count - current_count
            )
            (
                custom_sql_connection,
                total_count,
                has_next_page,
            ) = self.get_connection_object(
                custom_sql_graphql_query,
                "customSQLTablesConnection",
                custom_sql_filter,
                count,
                current_count,
            )
            current_count += count

            unique_custom_sql = get_unique_custom_sql(
                custom_sql_connection.get("nodes", [])
            )
            for csql in unique_custom_sql:
                csql_id: str = csql.get("id", "")
                csql_urn = builder.make_dataset_urn(
                    self.platform, csql_id, self.config.env
                )
                dataset_snapshot = DatasetSnapshot(
                    urn=csql_urn,
                    aspects=[],
                )

                # lineage from datasource -> custom sql source #
                yield from self._create_lineage_from_csql_datasource(
                    csql_urn, csql.get("datasources", [])
                )

                # lineage from custom sql -> datasets/tables #
                columns = csql.get("columns", [])
                yield from self._create_lineage_to_upstream_tables(csql_urn, columns)

                #  Schema Metadata
                schema_metadata = self.get_schema_metadata_for_custom_sql(columns)
                if schema_metadata is not None:
                    dataset_snapshot.aspects.append(schema_metadata)

                # Browse path
                browse_paths = BrowsePathsClass(
                    paths=[
                        f"/{self.config.env.lower()}/{self.platform}/Custom SQL/{csql.get('name', '')}/{csql_id}"
                    ]
                )
                dataset_snapshot.aspects.append(browse_paths)

                dataset_properties = DatasetPropertiesClass(
                    name=csql.get("name"), description=csql.get("description")
                )

                dataset_snapshot.aspects.append(dataset_properties)

                view_properties = ViewPropertiesClass(
                    materialized=False,
                    viewLanguage="SQL",
                    viewLogic=clean_query(csql.get("query", "")),
                )
                dataset_snapshot.aspects.append(view_properties)

                yield self.get_metadata_change_event(dataset_snapshot)
                yield self.get_metadata_change_proposal(
                    dataset_snapshot.urn,
                    aspect_name="subTypes",
                    aspect=SubTypesClass(typeNames=["View", "Custom SQL"]),
                )

    def get_schema_metadata_for_custom_sql(
        self, columns: List[dict]
    ) -> Optional[SchemaMetadata]:
        schema_metadata = None
        for field in columns:
            # Datasource fields
            fields = []
            nativeDataType = field.get("remoteType", "UNKNOWN")
            TypeClass = FIELD_TYPE_MAPPING.get(nativeDataType, NullTypeClass)
            schema_field = SchemaField(
                fieldPath=field.get("name", ""),
                type=SchemaFieldDataType(type=TypeClass()),
                nativeDataType=nativeDataType,
                description=field.get("description", ""),
            )
            fields.append(schema_field)

            schema_metadata = SchemaMetadata(
                schemaName="test",
                platform=f"urn:li:dataPlatform:{self.platform}",
                version=0,
                fields=fields,
                hash="",
                platformSchema=OtherSchema(rawSchema=""),
            )
        return schema_metadata

    def _create_lineage_from_csql_datasource(
        self, csql_urn: str, csql_datasource: List[dict]
    ) -> Iterable[MetadataWorkUnit]:
        for datasource in csql_datasource:
            datasource_urn = builder.make_dataset_urn(
                self.platform, datasource.get("id", ""), self.config.env
            )
            upstream_csql = UpstreamClass(
                dataset=csql_urn,
                type=DatasetLineageTypeClass.TRANSFORMED,
            )

            upstream_lineage = UpstreamLineage(upstreams=[upstream_csql])
            yield self.get_metadata_change_proposal(
                datasource_urn, aspect_name="upstreamLineage", aspect=upstream_lineage
            )

    def _create_lineage_to_upstream_tables(
        self, csql_urn: str, columns: List[dict]
    ) -> Iterable[MetadataWorkUnit]:
        used_datasources = []
        # Get data sources from columns' reference fields.
        for field in columns:
            data_sources = [
                reference.get("datasource")
                for reference in field.get("referencedByFields", {})
                if reference.get("datasource") is not None
            ]

            for datasource in data_sources:
                if datasource.get("id", "") in used_datasources:
                    continue
                used_datasources.append(datasource.get("id", ""))
                upstream_tables = self._create_upstream_table_lineage(
                    datasource,
                    datasource.get("workbook", {}).get("projectName", ""),
                    True,
                )
                if upstream_tables:
                    upstream_lineage = UpstreamLineage(upstreams=upstream_tables)
                    yield self.get_metadata_change_proposal(
                        csql_urn,
                        aspect_name="upstreamLineage",
                        aspect=upstream_lineage,
                    )

    def _get_schema_metadata_for_embedded_datasource(
        self, datasource_fields: List[dict]
    ) -> Optional[SchemaMetadata]:
        fields = []
        schema_metadata = None
        for field in datasource_fields:
            # check datasource - custom sql relations from a field being referenced
            self._track_custom_sql_ids(field)

            nativeDataType = field.get("dataType", "UNKNOWN")
            TypeClass = FIELD_TYPE_MAPPING.get(nativeDataType, NullTypeClass)

            schema_field = SchemaField(
                fieldPath=field["name"],
                type=SchemaFieldDataType(type=TypeClass()),
                description=make_description_from_params(
                    field.get("description", ""), field.get("formula")
                ),
                nativeDataType=nativeDataType,
                globalTags=get_tags_from_params(
                    [
                        field.get("role", ""),
                        field.get("__typename", ""),
                        field.get("aggregation", ""),
                    ]
                )
                if self.config.ingest_tags
                else None,
            )
            fields.append(schema_field)

        if fields:
            schema_metadata = SchemaMetadata(
                schemaName="test",
                platform=f"urn:li:dataPlatform:{self.platform}",
                version=0,
                fields=fields,
                hash="",
                platformSchema=OtherSchema(rawSchema=""),
            )

        return schema_metadata

    def get_metadata_change_event(
        self, snap_shot: Union["DatasetSnapshot", "DashboardSnapshot", "ChartSnapshot"]
    ) -> MetadataWorkUnit:
        mce = MetadataChangeEvent(proposedSnapshot=snap_shot)
        work_unit = MetadataWorkUnit(id=snap_shot.urn, mce=mce)
        self.report.report_workunit(work_unit)
        return work_unit

    def get_metadata_change_proposal(
        self,
        urn: str,
        aspect_name: str,
        aspect: Union["UpstreamLineage", "SubTypesClass"],
    ) -> MetadataWorkUnit:
        mcp = MetadataChangeProposalWrapper(
            entityType="dataset",
            changeType=ChangeTypeClass.UPSERT,
            entityUrn=urn,
            aspectName=aspect_name,
            aspect=aspect,
        )
        mcp_workunit = MetadataWorkUnit(
            id=f"tableau-{mcp.entityUrn}-{mcp.aspectName}",
            mcp=mcp,
            treat_errors_as_warnings=True,
        )
        self.report.report_workunit(mcp_workunit)
        return mcp_workunit

    def emit_datasource(
        self, datasource: dict, workbook: dict = None
    ) -> Iterable[MetadataWorkUnit]:
        datasource_info = workbook
        if workbook is None:
            datasource_info = datasource

        project = (
            datasource_info.get("projectName", "").replace("/", REPLACE_SLASH_CHAR)
            if datasource_info
            else ""
        )
        datasource_id = datasource.get("id", "")
        datasource_name = f"{datasource.get('name')}.{datasource_id}"
        datasource_urn = builder.make_dataset_urn(
            self.platform, datasource_id, self.config.env
        )
        if datasource_id not in self.datasource_ids_being_used:
            self.datasource_ids_being_used.append(datasource_id)

        dataset_snapshot = DatasetSnapshot(
            urn=datasource_urn,
            aspects=[],
        )

        # Browse path
        browse_paths = BrowsePathsClass(
            paths=[
                f"/{self.config.env.lower()}/{self.platform}/{project}/{datasource.get('name', '')}/{datasource_name}"
            ]
        )
        dataset_snapshot.aspects.append(browse_paths)

        # Ownership
        owner = (
            self._get_ownership(datasource_info.get("owner", {}).get("username", ""))
            if datasource_info
            else None
        )
        if owner is not None:
            dataset_snapshot.aspects.append(owner)

        # Dataset properties
        dataset_props = DatasetPropertiesClass(
            name=datasource.get("name"),
            description=datasource.get("description"),
            customProperties={
                "hasExtracts": str(datasource.get("hasExtracts", "")),
                "extractLastRefreshTime": datasource.get("extractLastRefreshTime", "")
                or "",
                "extractLastIncrementalUpdateTime": datasource.get(
                    "extractLastIncrementalUpdateTime", ""
                )
                or "",
                "extractLastUpdateTime": datasource.get("extractLastUpdateTime", "")
                or "",
                "type": datasource.get("__typename", ""),
            },
        )
        dataset_snapshot.aspects.append(dataset_props)

        # Upstream Tables
        if datasource.get("upstreamTables") is not None:
            # datasource -> db table relations
            upstream_tables = self._create_upstream_table_lineage(datasource, project)

            if upstream_tables:
                upstream_lineage = UpstreamLineage(upstreams=upstream_tables)
                yield self.get_metadata_change_proposal(
                    datasource_urn,
                    aspect_name="upstreamLineage",
                    aspect=upstream_lineage,
                )

        # Datasource Fields
        schema_metadata = self._get_schema_metadata_for_embedded_datasource(
            datasource.get("fields", [])
        )
        if schema_metadata is not None:
            dataset_snapshot.aspects.append(schema_metadata)

        yield self.get_metadata_change_event(dataset_snapshot)
        yield self.get_metadata_change_proposal(
            dataset_snapshot.urn,
            aspect_name="subTypes",
            aspect=SubTypesClass(typeNames=["Data Source"]),
        )

        if datasource.get("__typename") == "EmbeddedDatasource":
            yield from add_entity_to_container(
                self.gen_workbook_key(workbook), "dataset", dataset_snapshot.urn
            )

    def emit_published_datasources(self) -> Iterable[MetadataWorkUnit]:
        count_on_query = len(self.datasource_ids_being_used)
        datasource_filter = "idWithin: {}".format(
            json.dumps(self.datasource_ids_being_used)
        )
        (
            published_datasource_conn,
            total_count,
            has_next_page,
        ) = self.get_connection_object(
            published_datasource_graphql_query,
            "publishedDatasourcesConnection",
            datasource_filter,
        )

        current_count = 0
        while has_next_page:
            count = (
                count_on_query
                if current_count + count_on_query < total_count
                else total_count - current_count
            )
            (
                published_datasource_conn,
                total_count,
                has_next_page,
            ) = self.get_connection_object(
                published_datasource_graphql_query,
                "publishedDatasourcesConnection",
                datasource_filter,
                count,
                current_count,
            )

            current_count += count
            for datasource in published_datasource_conn.get("nodes", []):
                yield from self.emit_datasource(datasource)

    def emit_upstream_tables(self) -> Iterable[MetadataWorkUnit]:
        for (table_urn, (columns, path)) in self.upstream_tables.items():
            dataset_snapshot = DatasetSnapshot(
                urn=table_urn,
                aspects=[],
            )
            # Browse path
            browse_paths = BrowsePathsClass(
                paths=[f"/{self.config.env.lower()}/{self.platform}/{path}"]
            )
            dataset_snapshot.aspects.append(browse_paths)

            fields = []
            for field in columns:
                nativeDataType = field.get("remoteType", "UNKNOWN")
                TypeClass = FIELD_TYPE_MAPPING.get(nativeDataType, NullTypeClass)

                schema_field = SchemaField(
                    fieldPath=field["name"],
                    type=SchemaFieldDataType(type=TypeClass()),
                    description="",
                    nativeDataType=nativeDataType,
                )

                fields.append(schema_field)

            schema_metadata = SchemaMetadata(
                schemaName="test",
                platform=f"urn:li:dataPlatform:{self.platform}",
                version=0,
                fields=fields,
                hash="",
                platformSchema=OtherSchema(rawSchema=""),
            )
            if schema_metadata is not None:
                dataset_snapshot.aspects.append(schema_metadata)

            yield self.get_metadata_change_event(dataset_snapshot)

    def emit_sheets_as_charts(self, workbook: Dict) -> Iterable[MetadataWorkUnit]:
        for sheet in workbook.get("sheets", []):
            chart_snapshot = ChartSnapshot(
                urn=builder.make_chart_urn(self.platform, sheet.get("id")),
                aspects=[],
            )

            creator = workbook.get("owner", {}).get("username", "")
            created_at = sheet.get("createdAt", datetime.now())
            updated_at = sheet.get("updatedAt", datetime.now())
            last_modified = self.get_last_modified(creator, created_at, updated_at)

            if sheet.get("path"):
                site_part = f"/site/{self.config.site}" if self.config.site else ""
                sheet_external_url = (
                    f"{self.config.connect_uri}/#{site_part}/views/{sheet.get('path')}"
                )
            elif sheet.get("containedInDashboards"):
                # sheet contained in dashboard
                site_part = f"/t/{self.config.site}" if self.config.site else ""
                dashboard_path = sheet.get("containedInDashboards")[0].get("path", "")
                sheet_external_url = f"{self.config.connect_uri}{site_part}/authoring/{dashboard_path}/{sheet.get('name', '')}"
            else:
                # hidden or viz-in-tooltip sheet
                sheet_external_url = None
            fields = {}
            for field in sheet.get("datasourceFields", ""):
                description = make_description_from_params(
                    get_field_value_in_sheet(field, "description"),
                    get_field_value_in_sheet(field, "formula"),
                )
                fields[get_field_value_in_sheet(field, "name")] = description

            # datasource urn
            datasource_urn = []
            data_sources = sheet.get("upstreamDatasources", [])
            for datasource in data_sources:
                ds_id = datasource.get("id")
                if ds_id is None or not ds_id:
                    continue
                ds_urn = builder.make_dataset_urn(self.platform, ds_id, self.config.env)
                datasource_urn.append(ds_urn)
                if ds_id not in self.datasource_ids_being_used:
                    self.datasource_ids_being_used.append(ds_id)

            # Chart Info
            chart_info = ChartInfoClass(
                description="",
                title=sheet.get("name", ""),
                lastModified=last_modified,
                externalUrl=sheet_external_url,
                inputs=datasource_urn,
                customProperties=fields,
            )
            chart_snapshot.aspects.append(chart_info)

            # Browse path
            browse_path = BrowsePathsClass(
                paths=[
                    f"/{self.platform}/{workbook.get('projectName', '').replace('/', REPLACE_SLASH_CHAR)}"
                    f"/{workbook.get('name', '')}"
                    f"/{sheet.get('name', '').replace('/', REPLACE_SLASH_CHAR)}"
                ]
            )
            chart_snapshot.aspects.append(browse_path)

            # Ownership
            owner = self._get_ownership(creator)
            if owner is not None:
                chart_snapshot.aspects.append(owner)

            #  Tags
            tag_list = sheet.get("tags", [])
            if tag_list and self.config.ingest_tags:
                tag_list_str = [
                    t.get("name", "").upper() for t in tag_list if t is not None
                ]
                chart_snapshot.aspects.append(
                    builder.make_global_tag_aspect_with_tag_list(tag_list_str)
                )

            yield self.get_metadata_change_event(chart_snapshot)

            yield from add_entity_to_container(
                self.gen_workbook_key(workbook), "chart", chart_snapshot.urn
            )

    def emit_workbook_as_container(self, workbook: Dict) -> Iterable[MetadataWorkUnit]:

        workbook_container_key = self.gen_workbook_key(workbook)
        creator = workbook.get("owner", {}).get("username")

        owner_urn = (
            builder.make_user_urn(creator)
            if (creator and self.config.ingest_owner)
            else None
        )

        site_part = f"/site/{self.config.site}" if self.config.site else ""
        workbook_uri = workbook.get("uri", "")
        workbook_part = (
            workbook_uri[workbook_uri.index("/workbooks/") :]
            if workbook.get("uri")
            else None
        )
        workbook_external_url = (
            f"{self.config.connect_uri}/#{site_part}{workbook_part}"
            if workbook_part
            else None
        )

        tag_list = workbook.get("tags", [])
        tag_list_str = (
            [t.get("name", "").upper() for t in tag_list if t is not None]
            if (tag_list and self.config.ingest_tags)
            else None
        )

        container_workunits = gen_containers(
            container_key=workbook_container_key,
            name=workbook.get("name", ""),
            sub_types=["Workbook"],
            description=workbook.get("description"),
            owner_urn=owner_urn,
            external_url=workbook_external_url,
            tags=tag_list_str,
        )

        for wu in container_workunits:
            self.report.report_workunit(wu)
            yield wu

    def gen_workbook_key(self, workbook):
        return WorkbookKey(
            platform=self.platform, instance=None, workbook_id=workbook["id"]
        )

    def emit_dashboards(self, workbook: Dict) -> Iterable[MetadataWorkUnit]:
        for dashboard in workbook.get("dashboards", []):
            dashboard_snapshot = DashboardSnapshot(
                urn=builder.make_dashboard_urn(self.platform, dashboard.get("id", "")),
                aspects=[],
            )

            creator = workbook.get("owner", {}).get("username", "")
            created_at = dashboard.get("createdAt", datetime.now())
            updated_at = dashboard.get("updatedAt", datetime.now())
            last_modified = self.get_last_modified(creator, created_at, updated_at)

            site_part = f"/site/{self.config.site}" if self.config.site else ""
            dashboard_external_url = f"{self.config.connect_uri}/#{site_part}/views/{dashboard.get('path', '')}"
            title = dashboard.get("name", "").replace("/", REPLACE_SLASH_CHAR) or ""
            chart_urns = [
                builder.make_chart_urn(self.platform, sheet.get("id"))
                for sheet in dashboard.get("sheets", [])
            ]
            dashboard_info_class = DashboardInfoClass(
                description="",
                title=title,
                charts=chart_urns,
                lastModified=last_modified,
                dashboardUrl=dashboard_external_url,
                customProperties={},
            )
            dashboard_snapshot.aspects.append(dashboard_info_class)

            # browse path
            browse_paths = BrowsePathsClass(
                paths=[
                    f"/{self.platform}/{workbook.get('projectName', '').replace('/', REPLACE_SLASH_CHAR)}"
                    f"/{workbook.get('name', '').replace('/', REPLACE_SLASH_CHAR)}"
                    f"/{title}"
                ]
            )
            dashboard_snapshot.aspects.append(browse_paths)

            # Ownership
            owner = self._get_ownership(creator)
            if owner is not None:
                dashboard_snapshot.aspects.append(owner)

            yield self.get_metadata_change_event(dashboard_snapshot)

            yield from add_entity_to_container(
                self.gen_workbook_key(workbook), "dashboard", dashboard_snapshot.urn
            )

    def emit_embedded_datasource(self, workbook: Dict) -> Iterable[MetadataWorkUnit]:
        for datasource in workbook.get("embeddedDatasources", []):
            yield from self.emit_datasource(datasource, workbook)

    @lru_cache(maxsize=None)
    def _get_schema(self, schema_provided: str, database: str) -> str:
        schema = schema_provided
        if not schema_provided and database in self.config.default_schema_map:
            schema = self.config.default_schema_map[database]

        return schema

    @lru_cache(maxsize=None)
    def get_last_modified(
        self, creator: str, created_at: bytes, updated_at: bytes
    ) -> ChangeAuditStamps:
        last_modified = ChangeAuditStamps()
        if creator:
            modified_actor = builder.make_user_urn(creator)
            created_ts = int(dp.parse(created_at).timestamp() * 1000)
            modified_ts = int(dp.parse(updated_at).timestamp() * 1000)
            last_modified = ChangeAuditStamps(
                created=AuditStamp(time=created_ts, actor=modified_actor),
                lastModified=AuditStamp(time=modified_ts, actor=modified_actor),
            )
        return last_modified

    @lru_cache(maxsize=None)
    def _get_ownership(self, user: str) -> Optional[OwnershipClass]:
        if self.config.ingest_owner and user:
            owner_urn = builder.make_user_urn(user)
            ownership: OwnershipClass = OwnershipClass(
                owners=[
                    OwnerClass(
                        owner=owner_urn,
                        type=OwnershipTypeClass.DATAOWNER,
                    )
                ]
            )
            return ownership

        return None

    @classmethod
    def create(cls, config_dict: dict, ctx: PipelineContext) -> Source:
        config = TableauConfig.parse_obj(config_dict)
        return cls(ctx, config)

    def get_workunits(self) -> Iterable[MetadataWorkUnit]:
        try:
            yield from self.emit_workbooks(self.config.workbooks_page_size)
            if self.datasource_ids_being_used:
                yield from self.emit_published_datasources()
            if self.custom_sql_ids_being_used:
                yield from self.emit_custom_sql_datasources()
        except MetadataQueryException as md_exception:
            self.report.report_failure(
                key="tableau-metadata",
                reason=f"Unable to retrieve metadata from tableau. Information: {str(md_exception)}",
            )

    def get_report(self) -> SourceReport:
        return self.report
